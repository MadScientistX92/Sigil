"""Signed C2PA manifests (the metadata layer) and reading other providers' manifests."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path

import c2pa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

AI_SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
COMPOSITE_SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia"
DISCLOSURE_LABEL = "com.sigil.disclosure"
MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sigil Dev")])


def ensure_dev_signer(cert_dir: Path) -> tuple[bytes, bytes]:
    """Create (once) a development CA + ES256 signing cert. Production swaps in a real cert."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    chain_p, key_p = cert_dir / "chain.pem", cert_dir / "signer.key"
    if chain_p.exists() and key_p.exists():
        return chain_p.read_bytes(), key_p.read_bytes()
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("Sigil Dev Root CA")).issuer_name(_name("Sigil Dev Root CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(_name("Sigil Dev Signer")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=730))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    chain = leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)
    pkey = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    chain_p.write_bytes(chain)
    key_p.write_bytes(pkey)
    os.chmod(key_p, 0o600)
    return chain, pkey


def sign(data: bytes, fmt: str, disclosure: dict, cert_dir: Path, composite: bool = False) -> bytes:
    """Attach a signed C2PA manifest declaring AI origin plus Sigil's disclosure assertion."""
    chain, pkey = ensure_dev_signer(cert_dir)
    manifest = {
        "claim_generator_info": [{"name": "Sigil", "version": "0.1.0"}],
        "title": disclosure.get("title") or f"{disclosure.get('system', 'AI output')}",
        "assertions": [
            {"label": "c2pa.actions", "data": {"actions": [{
                "action": "c2pa.created",
                "digitalSourceType": COMPOSITE_SOURCE_TYPE if composite else AI_SOURCE_TYPE,
                "softwareAgent": {"name": disclosure.get("system", "unknown"),
                                  "version": disclosure.get("system_version", "")},
            }]}},
            {"label": DISCLOSURE_LABEL, "data": disclosure},
        ],
    }
    info = c2pa.C2paSignerInfo(alg=b"es256", sign_cert=chain, private_key=pkey, ta_url=None)
    signer = c2pa.Signer.from_info(info)
    builder = c2pa.Builder(json.dumps(manifest))
    src, dst = io.BytesIO(data), io.BytesIO()
    builder.sign(signer, MIME[fmt], src, dst)
    return dst.getvalue()


def read(data: bytes, fmt: str) -> dict:
    """Read any C2PA manifest (ours or another provider's). Returns a summary dict."""
    out = {"present": False, "ai_generated": False, "issuer": None, "generator": None,
           "valid_signature": None, "sigil": None, "validation": []}
    if fmt not in MIME:
        return out
    try:
        reader = c2pa.Reader(MIME[fmt], io.BytesIO(data))
        store = json.loads(reader.json())
    except Exception as e:  # no manifest or unreadable
        out["error"] = str(e)[:200]
        return out
    active = store.get("manifests", {}).get(store.get("active_manifest", ""), {})
    out["present"] = bool(active)
    out["issuer"] = (active.get("signature_info") or {}).get("issuer")
    gens = active.get("claim_generator_info") or []
    out["generator"] = gens[0].get("name") if gens else active.get("claim_generator")
    statuses = store.get("validation_status") or []
    out["validation"] = [s.get("code") for s in statuses]
    # Untrusted dev certs are expected during development; tampering codes are not.
    tamper = [c for c in out["validation"] if c and ("mismatch" in c or "invalid" in c and "untrusted" not in c)]
    out["valid_signature"] = not tamper
    for a in active.get("assertions", []):
        label = a.get("label", "")
        if label.startswith("c2pa.actions"):
            for act in (a.get("data") or {}).get("actions", []):
                if "trainedAlgorithmicMedia" in (act.get("digitalSourceType") or ""):
                    out["ai_generated"] = True
        if label == DISCLOSURE_LABEL:
            out["sigil"] = a.get("data")
    return out
