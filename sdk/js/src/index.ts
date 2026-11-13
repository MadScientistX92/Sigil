/**
 * Sigil JS/TS SDK. Zero dependencies: uses the platform `fetch`, `FormData` and `Blob`, so it runs on
 * Node 18+, browsers, Next.js (Node and Edge runtimes), Deno, Bun and Cloudflare Workers.
 *
 *   import { Sigil } from "@sigil-ai/sdk";
 *   const sigil = new Sigil({ apiKey: process.env.SIGIL_API_KEY, baseUrl: process.env.SIGIL_URL });
 *   const { data, receipt } = await sigil.markImage(png, { provider: "Acme AI", system: "acme-img" });
 */

export type Jurisdiction = "eu" | "ca" | "in" | "kr";
export type Modality = "image" | "text" | "audio" | "video";
export type FileInput = Blob | ArrayBuffer | ArrayBufferView;

export interface SigilOptions {
  /** `sk_sigil_...` key. Optional for the public endpoints (`verify`, `rules`, `record`). */
  apiKey?: string;
  /** Sigil server URL. Defaults to http://localhost:8000. */
  baseUrl?: string;
  /** Custom fetch (tests, proxies, older runtimes). Defaults to globalThis.fetch. */
  fetch?: typeof fetch;
  /** Abort a request after this many milliseconds. 0 disables. Default 30000. */
  timeoutMs?: number;
}

export interface Rule {
  id: string;
  jurisdiction: string;
  [k: string]: unknown;
}

export interface Receipt {
  content_id: string;
  modality: string;
  controls_applied: string[];
  rules: Rule[];
  still_required: Record<string, string>;
  verify_url: string | null;
  ruleset: string;
}

export interface MarkCommon {
  provider: string;
  system: string;
  systemVersion?: string;
  jurisdictions?: Jurisdiction[] | string;
}

export interface MarkImageOptions extends MarkCommon {
  deepfake?: boolean;
  monthlyUsers?: number;
  /** "auto" (default) lets the rules decide whether a visible label is required. */
  visibleLabel?: boolean | "auto";
  labelLang?: string;
  outputFormat?: "PNG" | "JPEG" | "WEBP";
  /** AI edit of a real photo, rather than fully generated. */
  composite?: boolean;
  filename?: string;
}

export interface MarkImageResult {
  /** The marked file bytes. */
  data: Uint8Array;
  contentType: string;
  contentId: string;
  receipt: Receipt;
}

export interface MarkTextOptions extends MarkCommon {
  /** Append a localized "AI-generated" disclosure line. */
  disclose?: boolean;
  labelLang?: string;
}

export interface MarkTextResult {
  text: string;
  receipt: Receipt;
}

export interface VerifyOptions {
  jurisdictions?: Jurisdiction[] | string;
  deepfake?: boolean;
  monthlyUsers?: number;
  filename?: string;
}

export interface VerifyReport {
  ai_generated: boolean;
  verdict: string;
  layers: Record<string, string>;
  record: Record<string, unknown> | null;
  compliance?: Record<string, unknown>;
  c2pa?: Record<string, unknown>;
  watermark?: { found: boolean; content_id?: string; [k: string]: unknown };
  match_score?: number;
  [k: string]: unknown;
}

export interface RulesQuery {
  modality?: Modality;
  jurisdictions?: Jurisdiction[] | string;
  deepfake?: boolean;
  monthlyUsers?: number;
}

export interface RulesResult {
  ruleset: string;
  required_controls: unknown;
  rules: (Rule & { status: string })[];
  disclaimer: string;
}

export interface ApiKey {
  id: string;
  org: string;
  name: string;
  /** Only present right after creation. Sigil stores just the hash. */
  key?: string;
  [k: string]: unknown;
}

export interface Usage {
  org: string;
  period: string;
  /** e.g. { "mark.image": 12, "verify.image": 40 } */
  metrics: Record<string, number>;
  total: number;
}

export interface RateLimitInfo {
  limit: number;
  remaining: number;
}

/** Thrown for any non-2xx response. `retryAfter` (seconds) is set on 429. */
export class SigilError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly retryAfter?: number;

  constructor(status: number, detail: unknown, retryAfter?: number) {
    const msg = typeof detail === "string" ? detail : JSON.stringify(detail);
    super(`Sigil API ${status}: ${msg}`);
    this.name = "SigilError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

const DEFAULT_URL = "http://localhost:8000";

function juris(j: Jurisdiction[] | string | undefined): string {
  if (j === undefined) return "eu,ca,in,kr";
  return Array.isArray(j) ? j.join(",") : j;
}

function toBlob(input: FileInput): Blob {
  if (input instanceof Blob) return input;
  if (input instanceof ArrayBuffer) return new Blob([input]);
  const v = input as ArrayBufferView;
  // Copy into a plain ArrayBuffer so SharedArrayBuffer-backed views and offsets are handled.
  return new Blob([new Uint8Array(v.buffer, v.byteOffset, v.byteLength).slice()]);
}

/** Decode the base64 JSON in X-Sigil-Receipt (UTF-8 safe, no Buffer needed). */
export function decodeReceipt(b64: string): Receipt {
  const bin = atob(b64);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

export class Sigil {
  readonly baseUrl: string;
  /** Rate-limit headers from the most recent `verify` call. */
  lastRateLimit?: RateLimitInfo;
  private readonly apiKey?: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  readonly keys = {
    create: (opts: { name?: string; org?: string } = {}): Promise<ApiKey> =>
      this.json("POST", "/v1/keys", { body: JSON.stringify(opts), auth: true, contentType: "application/json" }),
    list: async (org?: string): Promise<ApiKey[]> =>
      (await this.json<{ keys: ApiKey[] }>("GET", "/v1/keys" + (org ? `?org=${encodeURIComponent(org)}` : ""),
        { auth: true })).keys,
    revoke: (id: string): Promise<{ revoked: string }> =>
      this.json("DELETE", `/v1/keys/${encodeURIComponent(id)}`, { auth: true }),
  };

  constructor(opts: SigilOptions = {}) {
    this.apiKey = opts.apiKey || undefined;
    this.baseUrl = (opts.baseUrl || DEFAULT_URL).replace(/\/+$/, "");
    const f = opts.fetch ?? globalThis.fetch;
    if (!f) throw new Error("no fetch available; pass `fetch` in SigilOptions (Node 18+ has it built in)");
    this.fetchImpl = f.bind(globalThis);
    this.timeoutMs = opts.timeoutMs ?? 30000;
  }

  /** Watermark + C2PA-sign + (if required) visibly label an image. Needs an API key. */
  async markImage(file: FileInput, opts: MarkImageOptions): Promise<MarkImageResult> {
    const fd = new FormData();
    fd.append("file", toBlob(file), opts.filename ?? "image");
    fd.append("provider", opts.provider);
    fd.append("system", opts.system);
    if (opts.systemVersion) fd.append("system_version", opts.systemVersion);
    fd.append("jurisdictions", juris(opts.jurisdictions));
    if (opts.deepfake) fd.append("deepfake", "true");
    if (opts.monthlyUsers) fd.append("monthly_users", String(opts.monthlyUsers));
    if (opts.visibleLabel !== undefined && opts.visibleLabel !== "auto")
      fd.append("visible_label", String(opts.visibleLabel));
    if (opts.labelLang) fd.append("label_lang", opts.labelLang);
    if (opts.outputFormat) fd.append("output_format", opts.outputFormat);
    if (opts.composite) fd.append("composite", "true");

    const res = await this.request("POST", "/v1/mark/image", { body: fd, auth: true });
    const rh = res.headers.get("x-sigil-receipt");
    if (!rh) throw new SigilError(res.status, "response is missing X-Sigil-Receipt");
    const receipt = decodeReceipt(rh);
    return {
      data: new Uint8Array(await res.arrayBuffer()),
      contentType: res.headers.get("content-type") ?? "application/octet-stream",
      contentId: res.headers.get("x-sigil-content-id") ?? receipt.content_id,
      receipt,
    };
  }

  /** Log a text fingerprint and optionally append a disclosure. Needs an API key. */
  markText(text: string, opts: MarkTextOptions): Promise<MarkTextResult> {
    const body = {
      text,
      provider: opts.provider,
      system: opts.system,
      system_version: opts.systemVersion ?? "",
      jurisdictions: juris(opts.jurisdictions),
      disclose: opts.disclose ?? false,
      label_lang: opts.labelLang ?? "en",
    };
    return this.json("POST", "/v1/mark/text", { body: JSON.stringify(body), auth: true,
      contentType: "application/json" });
  }

  /** Check a file (Blob/bytes) or a string of text. Public; a key raises the rate limit. */
  async verify(input: FileInput | string, opts: VerifyOptions = {}): Promise<VerifyReport> {
    const fd = new FormData();
    if (typeof input === "string") fd.append("text", input);
    else fd.append("file", toBlob(input), opts.filename ?? "file");
    fd.append("jurisdictions", juris(opts.jurisdictions));
    if (opts.deepfake) fd.append("deepfake", "true");
    if (opts.monthlyUsers) fd.append("monthly_users", String(opts.monthlyUsers));
    const res = await this.request("POST", "/v1/verify", { body: fd, auth: !!this.apiKey });
    const limit = res.headers.get("x-ratelimit-limit");
    const remaining = res.headers.get("x-ratelimit-remaining");
    if (limit !== null && remaining !== null) this.lastRateLimit = { limit: +limit, remaining: +remaining };
    return res.json();
  }

  /** Which controls apply. Public. */
  rules(q: RulesQuery = {}): Promise<RulesResult> {
    const p = new URLSearchParams({ modality: q.modality ?? "image", jurisdictions: juris(q.jurisdictions) });
    if (q.deepfake) p.set("deepfake", "true");
    if (q.monthlyUsers) p.set("monthly_users", String(q.monthlyUsers));
    return this.json("GET", `/v1/rules?${p}`);
  }

  /** Public provenance record for a content ID. */
  record(contentId: string): Promise<Record<string, unknown>> {
    return this.json("GET", `/v/${encodeURIComponent(contentId)}`);
  }

  /** Metered usage for your org. `period` is YYYY-MM (default: current month). */
  usage(period?: string): Promise<Usage> {
    return this.json("GET", "/v1/usage" + (period ? `?period=${encodeURIComponent(period)}` : ""), { auth: true });
  }

  /** Compliance evidence export (all marks + audit log) for your org. */
  evidence(): Promise<Record<string, unknown>> {
    return this.json("GET", "/v1/evidence", { auth: true });
  }

  private async json<T>(method: string, path: string, o: ReqOpts = {}): Promise<T> {
    return (await this.request(method, path, o)).json() as Promise<T>;
  }

  private async request(method: string, path: string, o: ReqOpts = {}): Promise<Response> {
    const headers: Record<string, string> = {};
    if (o.auth) {
      if (!this.apiKey) throw new SigilError(401, "this call needs an API key (new Sigil({ apiKey }))");
      headers.Authorization = `Bearer ${this.apiKey}`;
    }
    if (o.contentType) headers["Content-Type"] = o.contentType;
    const ctrl = this.timeoutMs > 0 ? new AbortController() : undefined;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), this.timeoutMs) : undefined;
    try {
      const res = await this.fetchImpl(this.baseUrl + path, { method, headers, body: o.body, signal: ctrl?.signal });
      if (!res.ok) {
        let detail: unknown = res.statusText;
        try {
          const j = await res.json();
          detail = (j && typeof j === "object" && "detail" in j) ? (j as { detail: unknown }).detail : j;
        } catch { /* non-JSON error body */ }
        const ra = res.headers.get("retry-after");
        throw new SigilError(res.status, detail, ra !== null ? Number(ra) : undefined);
      }
      return res;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}

interface ReqOpts {
  body?: BodyInit;
  auth?: boolean;
  contentType?: string;
}

export default Sigil;
