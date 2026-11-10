"""HTTP API. Run: uvicorn sigil.api:app --port 8000"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import policy
from .accounts import RateLimiter
from .core import MarkOptions, Sigil

MAX_BYTES = 25 * 1024 * 1024
sigil = Sigil(os.environ.get("SIGIL_DATA", "data"), os.environ.get("SIGIL_PUBLIC_URL", "http://localhost:8000"))
# Static bootstrap keys ("key:org,key:org"). Keys created via /v1/keys or `sigil keys create` live hashed in
# the database. Set SIGIL_API_KEYS="" in production to disable the default "dev" key.
KEYS = dict(kv.split(":", 1) for kv in os.environ.get("SIGIL_API_KEYS", "dev:default").split(",") if ":" in kv)
ADMIN_TOKEN = os.environ.get("SIGIL_ADMIN_TOKEN", "")
# Requests per minute on /v1/verify. 0 disables the limit.
VERIFY_RATE_ANON = int(os.environ.get("SIGIL_VERIFY_RATE_ANON", "30"))
VERIFY_RATE_KEYED = int(os.environ.get("SIGIL_VERIFY_RATE_KEYED", "600"))
# Only trust X-Forwarded-For behind a proxy you control (Fly, Render, nginx), or clients can spoof it.
TRUST_PROXY = os.environ.get("SIGIL_TRUST_PROXY", "") == "1"
limiter = RateLimiter()

app = FastAPI(title="Sigil", version="0.1.0",
              description="Make AI output compliant with labeling laws in the EU, California, India and Korea.")


def _bearer(authorization: str) -> str:
    return authorization.removeprefix("Bearer ").strip()


def _lookup(key: str) -> str | None:
    return sigil.accounts.org_for_key(key) or KEYS.get(key)


def _is_admin(key: str) -> bool:
    return bool(ADMIN_TOKEN) and hmac.compare_digest(key.encode(), ADMIN_TOKEN.encode())


def org_for(authorization: str = Header(default="")) -> str:
    org = _lookup(_bearer(authorization))
    if org is None:
        raise HTTPException(401, "invalid API key")
    return org


def optional_org(authorization: str = Header(default="")) -> str | None:
    key = _bearer(authorization)
    if not key:
        return None
    return org_for(authorization)


def key_admin(authorization: str = Header(default="")) -> str | None:
    """Org of the caller for key management, or None for the admin token."""
    key = _bearer(authorization)
    if _is_admin(key):
        return None
    return org_for(authorization)


def _client_ip(request: Request) -> str:
    if TRUST_PROXY and (fwd := request.headers.get("x-forwarded-for")):
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _juris(s: str) -> tuple[str, ...]:
    js = tuple(j.strip().lower() for j in s.split(",") if j.strip())
    bad = [j for j in js if j not in policy.JURISDICTIONS]
    if bad:
        raise HTTPException(400, f"unknown jurisdictions {bad}; use {list(policy.JURISDICTIONS)}")
    return js


async def _read(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file too large (25 MB max)")
    return data


@app.get("/health")
def health():
    return {"ok": True, "ruleset": policy.RULESET_VERSION}


@app.get("/v1/rules")
def rules(modality: str = "image", jurisdictions: str = "eu,ca,in,kr", deepfake: bool = False,
          monthly_users: int = 0):
    ctx = policy.Context(modality, _juris(jurisdictions), deepfake, monthly_users)
    return {"ruleset": policy.RULESET_VERSION,
            "required_controls": policy.required_controls(ctx),
            "rules": [dict(r.to_dict(), status=policy.rule_status(r)) for r in policy.applicable_rules(ctx)],
            "disclaimer": "Engineering guidance, not legal advice."}


@app.post("/v1/mark/image")
async def mark_image(file: UploadFile = File(...), provider: str = Form(...), system: str = Form(...),
                     system_version: str = Form(""), jurisdictions: str = Form("eu,ca,in,kr"),
                     deepfake: bool = Form(False), monthly_users: int = Form(0),
                     visible_label: str = Form("auto"), label_lang: str = Form("en"),
                     output_format: str = Form(""), composite: bool = Form(False), org: str = Depends(org_for)):
    data = await _read(file)
    opts = MarkOptions(provider=provider, system=system, system_version=system_version, org=org,
                       jurisdictions=_juris(jurisdictions), deepfake=deepfake, monthly_users=monthly_users,
                       visible_label=None if visible_label == "auto" else visible_label.lower() == "true",
                       label_lang=label_lang, output_format=output_format or None, composite=composite)
    try:
        out, receipt = sigil.mark_image(data, opts)
    except ValueError as e:
        raise HTTPException(422, str(e))
    from PIL import Image
    import io
    sigil.accounts.meter(org, "mark.image")
    fmt = (Image.open(io.BytesIO(out)).format or "PNG").lower()
    rj = json.dumps(receipt.to_dict())
    return Response(out, media_type=f"image/{fmt}", headers={
        "X-Sigil-Content-Id": receipt.content_id,
        "X-Sigil-Receipt": base64.b64encode(rj.encode()).decode()})


class TextIn(BaseModel):
    text: str
    provider: str
    system: str
    system_version: str = ""
    jurisdictions: str = "eu,ca,in,kr"
    disclose: bool = False
    label_lang: str = "en"


@app.post("/v1/mark/text")
def mark_text(body: TextIn, org: str = Depends(org_for)):
    opts = MarkOptions(provider=body.provider, system=body.system, system_version=body.system_version,
                       org=org, jurisdictions=_juris(body.jurisdictions), label_lang=body.label_lang)
    text, receipt = sigil.mark_text(body.text, opts, disclose=body.disclose)
    sigil.accounts.meter(org, "mark.text")
    return {"text": text, "receipt": receipt.to_dict()}


@app.post("/v1/verify")
async def verify(request: Request, response: Response, file: UploadFile | None = File(None),
                 text: str | None = Form(None), jurisdictions: str = Form("eu,ca,in,kr"),
                 deepfake: bool = Form(False), monthly_users: int = Form(0),
                 org: str | None = Depends(optional_org)):
    bucket, per_min = (f"org:{org}", VERIFY_RATE_KEYED) if org else (f"ip:{_client_ip(request)}", VERIFY_RATE_ANON)
    d = limiter.hit(bucket, per_min)
    rl = {"X-RateLimit-Limit": str(d.limit), "X-RateLimit-Remaining": str(d.remaining)}
    if not d.allowed:
        raise HTTPException(429, "rate limit exceeded; send an API key for a higher limit",
                            headers=rl | {"Retry-After": str(d.retry_after)})
    response.headers.update(rl)
    js = _juris(jurisdictions)
    if file is not None:
        data = await _read(file)
        try:
            rep = sigil.verify_image(data, policy.Context("image", js, deepfake, monthly_users))
        except Exception as e:
            raise HTTPException(422, f"could not read image: {e}")
        sigil.accounts.meter(org or "public", "verify.image")
        return rep
    if text:
        rep = sigil.verify_text(text, policy.Context("text", js, deepfake, monthly_users))
        sigil.accounts.meter(org or "public", "verify.text")
        return rep
    raise HTTPException(400, "send a file or text")


class KeyIn(BaseModel):
    name: str = ""
    org: str | None = None  # admin token only; org keys always create keys for their own org


@app.post("/v1/keys", status_code=201)
def create_key(body: KeyIn, caller: str | None = Depends(key_admin)):
    if caller is None and not body.org:
        raise HTTPException(400, "org is required with the admin token")
    if caller is not None and body.org not in (None, caller):
        raise HTTPException(403, "an org key can only create keys for its own org")
    k = sigil.accounts.create_key(body.org or caller, body.name)
    sigil.registry.audit(k["org"], "key.create", detail={"key_id": k["id"], "name": body.name})
    return k | {"note": "Store this key now. Sigil keeps only its hash."}


@app.get("/v1/keys")
def list_keys(org: str | None = None, caller: str | None = Depends(key_admin)):
    return {"keys": sigil.accounts.list_keys(caller if caller is not None else org)}


@app.delete("/v1/keys/{key_id}")
def revoke_key(key_id: str, caller: str | None = Depends(key_admin)):
    if not sigil.accounts.revoke_key(key_id, caller):
        raise HTTPException(404, "no active key with that id")
    sigil.registry.audit(caller or "admin", "key.revoke", detail={"key_id": key_id})
    return {"revoked": key_id}


@app.get("/v1/usage")
def usage(period: str | None = None, org: str = Depends(org_for)):
    if period and not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(400, "period must be YYYY-MM")
    return sigil.accounts.usage(org, period)


@app.get("/v/{content_id}")
def public_record(content_id: str):
    r = sigil.registry.get(content_id)
    if not r:
        raise HTTPException(404, "no record")
    return {k: r[k] for k in ("content_id", "modality", "provider", "system", "system_version",
                              "created_at", "jurisdictions")} | {"ai_generated": True}


@app.get("/v1/evidence")
def evidence(org: str = Depends(org_for)):
    return JSONResponse(sigil.registry.evidence(org))


@app.get("/verify", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def verify_page():
    return (Path(__file__).parent / "static" / "verify.html").read_text()
