"""HTTP API. Run: uvicorn sigil.api:app --port 8000"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import policy
from .core import MarkOptions, Sigil

MAX_BYTES = 25 * 1024 * 1024
sigil = Sigil(os.environ.get("SIGIL_DATA", "data"), os.environ.get("SIGIL_PUBLIC_URL", "http://localhost:8000"))
KEYS = dict(kv.split(":", 1) for kv in os.environ.get("SIGIL_API_KEYS", "dev:default").split(",") if ":" in kv)

app = FastAPI(title="Sigil", version="0.1.0",
              description="Make AI output compliant with labeling laws in the EU, California, India and Korea.")


def org_for(authorization: str = Header(default="")) -> str:
    key = authorization.removeprefix("Bearer ").strip()
    if key not in KEYS:
        raise HTTPException(401, "invalid API key")
    return KEYS[key]


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
    return {"text": text, "receipt": receipt.to_dict()}


@app.post("/v1/verify")
async def verify(file: UploadFile | None = File(None), text: str | None = Form(None),
                 jurisdictions: str = Form("eu,ca,in,kr"), deepfake: bool = Form(False),
                 monthly_users: int = Form(0)):
    js = _juris(jurisdictions)
    if file is not None:
        data = await _read(file)
        try:
            return sigil.verify_image(data, policy.Context("image", js, deepfake, monthly_users))
        except Exception as e:
            raise HTTPException(422, f"could not read image: {e}")
    if text:
        return sigil.verify_text(text, policy.Context("text", js, deepfake, monthly_users))
    raise HTTPException(400, "send a file or text")


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
