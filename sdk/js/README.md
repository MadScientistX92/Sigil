# @sigil-ai/sdk

TypeScript client for the [Sigil](../../README.md) API. Zero dependencies: it uses the platform `fetch`,
`FormData` and `Blob`, so it runs on Node 18+, browsers, Next.js (Node and Edge runtimes), Deno, Bun and
Cloudflare Workers.

> Not on npm yet. Until it is, build it from this folder: `npm install && npm run build`, then
> `npm install /path/to/sigil/sdk/js` in your app.

## Next.js: mark every generated image (one call)

```ts
// app/api/generate/route.ts
import { Sigil } from "@sigil-ai/sdk";

const sigil = new Sigil({ apiKey: process.env.SIGIL_API_KEY, baseUrl: process.env.SIGIL_URL });

export async function POST(req: Request) {
  const png = await generateImage(await req.json());          // your model call
  const { data, contentType, contentId } = await sigil.markImage(png, {
    provider: "Acme AI", system: "acme-img", jurisdictions: ["eu", "ca", "in", "kr"],
  });
  return new Response(data, { headers: { "Content-Type": contentType, "X-Sigil-Content-Id": contentId } });
}
```

Keep the API key on the server. Only `verify`, `rules` and `record` are safe to call from a browser.

## API

| Method | Server route | Key |
|---|---|---|
| `markImage(file, { provider, system, jurisdictions?, deepfake?, visibleLabel?, labelLang?, outputFormat?, composite? })` → `{ data, contentType, contentId, receipt }` | `POST /v1/mark/image` | yes |
| `markText(text, { provider, system, disclose?, labelLang? })` → `{ text, receipt }` | `POST /v1/mark/text` | yes |
| `verify(fileOrText, { jurisdictions?, deepfake?, monthlyUsers? })` → report | `POST /v1/verify` | optional (higher rate limit) |
| `rules({ modality?, jurisdictions?, deepfake?, monthlyUsers? })` | `GET /v1/rules` | no |
| `record(contentId)` | `GET /v/{id}` | no |
| `usage(period?)`, `evidence()` | `GET /v1/usage`, `GET /v1/evidence` | yes |
| `keys.create({ name? })`, `keys.list()`, `keys.revoke(id)` | `/v1/keys` | yes |

`file` can be a `Blob`/`File`, `ArrayBuffer`, `Uint8Array` or Node `Buffer`. `jurisdictions` takes an array or a
comma-separated string and defaults to all four.

Every non-2xx response throws `SigilError` with `status`, `detail` and, on 429, `retryAfter` (seconds). After a
`verify` call, `sigil.lastRateLimit` holds `{ limit, remaining }`. `timeoutMs` (default 30000) aborts slow requests.

## Tests

```bash
npm test                     # unit tests with a fake fetch
pytest -q tests/test_js_sdk.py   # from the repo root: unit + end-to-end against a live server
```

Apache-2.0.
