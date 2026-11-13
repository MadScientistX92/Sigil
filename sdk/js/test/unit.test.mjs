// Unit tests with a fake fetch: request shape, auth, receipt decoding, errors. Run: npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { Sigil, SigilError, decodeReceipt } from "../dist/index.js";

function fakeFetch(respond) {
  const calls = [];
  const f = async (url, init) => { calls.push({ url, init }); return respond(url, init); };
  f.calls = calls;
  return f;
}

const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", ...headers } });

const b64 = (s) => btoa(String.fromCharCode(...new TextEncoder().encode(s)));

test("markImage sends multipart with auth and decodes the receipt", async () => {
  const receipt = { content_id: "abc123", modality: "image", controls_applied: ["c2pa"], rules: [],
                    still_required: {}, verify_url: "http://x/v/abc123", ruleset: "2026.11", note: "생성형 AI" };
  const f = fakeFetch(() => new Response(new Uint8Array([1, 2, 3]), { headers: {
    "content-type": "image/png", "x-sigil-content-id": "abc123", "x-sigil-receipt": b64(JSON.stringify(receipt)) } }));
  const s = new Sigil({ apiKey: "sk_sigil_test", baseUrl: "http://api.test/", fetch: f });
  const out = await s.markImage(new Uint8Array([9, 9]), { provider: "Acme", system: "img",
    jurisdictions: ["eu", "kr"], deepfake: true, visibleLabel: false });
  assert.deepEqual([...out.data], [1, 2, 3]);
  assert.equal(out.contentId, "abc123");
  assert.equal(out.contentType, "image/png");
  assert.equal(out.receipt.note, "생성형 AI");  // UTF-8 survives base64
  const { url, init } = f.calls[0];
  assert.equal(url, "http://api.test/v1/mark/image");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer sk_sigil_test");
  const fd = init.body;
  assert.equal(fd.get("provider"), "Acme");
  assert.equal(fd.get("jurisdictions"), "eu,kr");
  assert.equal(fd.get("deepfake"), "true");
  assert.equal(fd.get("visible_label"), "false");
  assert.ok(fd.get("file") instanceof Blob);
});

test("auth-only calls fail fast without a key, public calls send no header", async () => {
  const f = fakeFetch(() => json({ ruleset: "r", required_controls: [], rules: [], disclaimer: "" }));
  const s = new Sigil({ fetch: f });
  await assert.rejects(s.markText("hi", { provider: "a", system: "b" }), (e) => e instanceof SigilError && e.status === 401);
  assert.equal(f.calls.length, 0);
  await s.rules({ modality: "text", jurisdictions: "eu", monthlyUsers: 2000000 });
  assert.equal(f.calls[0].url, "http://localhost:8000/v1/rules?modality=text&jurisdictions=eu&monthly_users=2000000");
  assert.equal(f.calls[0].init.headers.Authorization, undefined);
});

test("verify sends text, records rate-limit headers", async () => {
  const f = fakeFetch(() => json({ ai_generated: false, verdict: "none", layers: {}, record: null }, 200,
    { "x-ratelimit-limit": "30", "x-ratelimit-remaining": "29" }));
  const s = new Sigil({ fetch: f });
  const r = await s.verify("some text", { jurisdictions: ["eu"] });
  assert.equal(r.ai_generated, false);
  assert.deepEqual(s.lastRateLimit, { limit: 30, remaining: 29 });
  assert.equal(f.calls[0].init.body.get("text"), "some text");
  assert.equal(f.calls[0].init.body.get("file"), null);
});

test("errors surface status, detail and Retry-After", async () => {
  const f = fakeFetch(() => json({ detail: "rate limit exceeded" }, 429, { "retry-after": "7" }));
  const s = new Sigil({ fetch: f });
  await assert.rejects(s.verify("x"), (e) => {
    assert.ok(e instanceof SigilError);
    assert.equal(e.status, 429);
    assert.equal(e.detail, "rate limit exceeded");
    assert.equal(e.retryAfter, 7);
    return true;
  });
});

test("markText posts JSON with defaults", async () => {
  const f = fakeFetch(() => json({ text: "hi\n\nAI-generated", receipt: { content_id: "t1" } }));
  const s = new Sigil({ apiKey: "k", fetch: f });
  const r = await s.markText("hi", { provider: "a", system: "b", disclose: true });
  assert.equal(r.receipt.content_id, "t1");
  const body = JSON.parse(f.calls[0].init.body);
  assert.deepEqual(body, { text: "hi", provider: "a", system: "b", system_version: "",
                           jurisdictions: "eu,ca,in,kr", disclose: true, label_lang: "en" });
  assert.equal(f.calls[0].init.headers["Content-Type"], "application/json");
});

test("keys and usage hit the right paths", async () => {
  const f = fakeFetch(() => json({ keys: [{ id: "k1" }], revoked: "k1", org: "acme", period: "2026-11" }));
  const s = new Sigil({ apiKey: "k", fetch: f });
  assert.equal((await s.keys.list("acme"))[0].id, "k1");
  await s.keys.revoke("k/1");
  await s.usage("2026-11");
  assert.deepEqual(f.calls.map((c) => [c.init.method, c.url.replace("http://localhost:8000", "")]), [
    ["GET", "/v1/keys?org=acme"], ["DELETE", "/v1/keys/k%2F1"], ["GET", "/v1/usage?period=2026-11"]]);
});

test("decodeReceipt round-trips", () => {
  assert.deepEqual(decodeReceipt(b64('{"content_id":"z"}')), { content_id: "z" });
});
