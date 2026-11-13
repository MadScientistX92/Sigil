// End-to-end check against a running Sigil server. Driven by tests/test_js_sdk.py.
// Usage: node e2e.mjs <baseUrl> <apiKey> <png path>
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { Sigil, SigilError } from "../dist/index.js";

const [baseUrl, apiKey, pngPath] = process.argv.slice(2);
const sigil = new Sigil({ baseUrl, apiKey });
const pub = new Sigil({ baseUrl });

const rules = await pub.rules({ modality: "image", jurisdictions: ["eu", "kr"], deepfake: true });
assert.ok(rules.rules.length > 0 && rules.ruleset);

const png = readFileSync(pngPath);
const marked = await sigil.markImage(png, { provider: "Acme AI", system: "acme-img", jurisdictions: ["eu", "ca"],
                                            filename: "gen.png" });
assert.match(marked.contentType, /^image\//);
assert.equal(marked.contentId, marked.receipt.content_id);
assert.ok(marked.data.length > 1000);

const rep = await pub.verify(new Blob([marked.data], { type: marked.contentType }), { jurisdictions: ["eu"] });
assert.equal(rep.ai_generated, true);
assert.equal(rep.watermark.content_id, marked.contentId);
assert.ok(rep.compliance);
assert.ok(pub.lastRateLimit && pub.lastRateLimit.limit > 0);

const rec = await pub.record(marked.contentId);
assert.equal(rec.ai_generated, true);

const t = await sigil.markText("The quick brown fox jumps over the lazy dog near the river bank today.",
                               { provider: "Acme AI", system: "acme-llm", disclose: true });
assert.ok(t.text.length > 60 && t.receipt.content_id);
const tv = await pub.verify(t.text);
assert.equal(tv.ai_generated, true);

const k = await sigil.keys.create({ name: "ci" });
assert.match(k.key, /^sk_sigil_/);
const ci = new Sigil({ baseUrl, apiKey: k.key });
assert.ok((await ci.keys.list()).some((x) => x.id === k.id));
await ci.keys.revoke(k.id);
await assert.rejects(ci.usage(), (e) => e instanceof SigilError && e.status === 401);

const u = await sigil.usage();
assert.equal(u.metrics["mark.image"], 1);
assert.equal(u.metrics["mark.text"], 1);

await assert.rejects(pub.markImage(png, { provider: "a", system: "b" }), (e) => e.status === 401);
await assert.rejects(pub.verify("x", { jurisdictions: "mars" }), (e) => e.status === 400);

// Anonymous verify limit is set to 5/min by the test; we have used 3 so far.
let err;
for (let i = 0; i < 5 && !err; i++) await pub.verify("hello").catch((e) => { err = e; });
assert.ok(err instanceof SigilError && err.status === 429 && err.retryAfter > 0);

console.log("e2e ok");
