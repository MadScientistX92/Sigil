import time

import pytest
from fastapi.testclient import TestClient

from sigil.accounts import RateLimiter
from sigil.core import Sigil

from test_sigil import _img


@pytest.fixture
def sg(tmp_path):
    return Sigil(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import sigil.api as api
    s = Sigil(tmp_path)
    monkeypatch.setattr(api, "sigil", s)
    monkeypatch.setattr(api, "ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(api, "limiter", RateLimiter())
    return TestClient(api.app), s, api


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def test_keys_are_hashed_and_revocable(sg, tmp_path):
    k = sg.accounts.create_key("acme", "prod")
    assert k["key"].startswith("sk_sigil_")
    assert sg.accounts.org_for_key(k["key"]) == "acme"
    assert sg.accounts.org_for_key(k["key"] + "x") is None
    raw = (tmp_path / "sigil.db").read_bytes()
    assert k["key"].encode() not in raw  # only the hash is stored
    listed = sg.accounts.list_keys("acme")
    assert listed[0]["last_used_at"] and "key" not in listed[0] and "key_hash" not in listed[0]
    assert not sg.accounts.revoke_key(k["id"], org="other")  # cannot revoke another org's key
    assert sg.accounts.revoke_key(k["id"], org="acme")
    assert sg.accounts.org_for_key(k["key"]) is None


def test_usage_metering_by_month(sg):
    jan = time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, 0))
    sg.accounts.meter("acme", "mark.image", ts=jan)
    sg.accounts.meter("acme", "mark.image", ts=jan)
    sg.accounts.meter("acme", "verify.image", n=3, ts=jan)
    sg.accounts.meter("other", "mark.image", ts=jan)
    u = sg.accounts.usage("acme", "2026-01")
    assert u["metrics"] == {"mark.image": 2, "verify.image": 3} and u["total"] == 5
    assert sg.accounts.usage("acme", "2026-02")["total"] == 0


def test_rate_limiter_refills():
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    assert all(rl.hit("a", 3).allowed for _ in range(3))
    d = rl.hit("a", 3)
    assert not d.allowed and d.retry_after == 20  # 3/min -> one token every 20 s
    assert rl.hit("b", 3).allowed  # buckets are independent
    now[0] = 20.0
    assert rl.hit("a", 3).allowed and not rl.hit("a", 3).allowed
    assert all(rl.hit("c", 0).allowed for _ in range(100))  # 0 disables


def test_api_key_lifecycle(client):
    c, s, api = client
    assert c.post("/v1/keys", json={"org": "acme"}).status_code == 401
    assert c.post("/v1/keys", headers=_h("admin-secret"), json={}).status_code == 400
    r = c.post("/v1/keys", headers=_h("admin-secret"), json={"org": "acme", "name": "prod"})
    assert r.status_code == 201
    key, kid = r.json()["key"], r.json()["id"]
    # the new key works for marking, and an org key can mint keys only for its own org
    m = c.post("/v1/mark/image", headers=_h(key), files={"file": ("a.png", _img())},
               data={"provider": "Acme AI", "system": "acme-img"})
    assert m.status_code == 200
    assert c.post("/v1/keys", headers=_h(key), json={"org": "evil"}).status_code == 403
    k2 = c.post("/v1/keys", headers=_h(key), json={"name": "ci"}).json()
    assert k2["org"] == "acme"
    assert {k["id"] for k in c.get("/v1/keys", headers=_h(key)).json()["keys"]} == {kid, k2["id"]}
    # revoke, then the key stops working
    assert c.delete(f"/v1/keys/{kid}", headers=_h(k2["key"])).status_code == 200
    assert c.get("/v1/usage", headers=_h(key)).status_code == 401
    assert c.delete(f"/v1/keys/{kid}", headers=_h(k2["key"])).status_code == 404
    assert any(e["event"] == "key.create" for e in s.registry.evidence("acme")["audit"])


def test_api_meters_usage(client):
    c, s, api = client
    key = s.accounts.create_key("acme")["key"]
    m = c.post("/v1/mark/image", headers=_h(key), files={"file": ("a.png", _img())},
               data={"provider": "Acme AI", "system": "acme-img"})
    c.post("/v1/mark/text", headers=_h(key), json={"text": "hello there world", "provider": "a", "system": "b"})
    c.post("/v1/verify", headers=_h(key), files={"file": ("m.png", m.content)})
    c.post("/v1/verify", data={"text": "anonymous check"})
    u = c.get("/v1/usage", headers=_h(key)).json()
    assert u["metrics"] == {"mark.image": 1, "mark.text": 1, "verify.image": 1}
    assert s.accounts.usage("public")["metrics"] == {"verify.text": 1}
    assert c.get("/v1/usage?period=bad", headers=_h(key)).status_code == 400


def test_verify_rate_limited(client, monkeypatch):
    c, s, api = client
    monkeypatch.setattr(api, "VERIFY_RATE_ANON", 2)
    monkeypatch.setattr(api, "VERIFY_RATE_KEYED", 5)
    for _ in range(2):
        r = c.post("/v1/verify", data={"text": "is this ai"})
        assert r.status_code == 200 and r.headers["X-RateLimit-Limit"] == "2"
    r = c.post("/v1/verify", data={"text": "is this ai"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # a key gets its own, larger bucket
    key = s.accounts.create_key("acme")["key"]
    r = c.post("/v1/verify", headers=_h(key), data={"text": "is this ai"})
    assert r.status_code == 200 and r.headers["X-RateLimit-Limit"] == "5"
    # a bad key is rejected, not silently downgraded to anonymous
    assert c.post("/v1/verify", headers=_h("nope"), data={"text": "x"}).status_code == 401
    # X-Forwarded-For is ignored unless the proxy is trusted
    assert c.post("/v1/verify", headers={"X-Forwarded-For": "1.2.3.4"}, data={"text": "x"}).status_code == 429
    monkeypatch.setattr(api, "TRUST_PROXY", True)
    assert c.post("/v1/verify", headers={"X-Forwarded-For": "1.2.3.4"}, data={"text": "x"}).status_code == 200


def test_env_bootstrap_key_still_works(client):
    c, s, api = client
    assert c.get("/v1/usage", headers=_h("dev")).json()["org"] == "default"
    assert c.get("/v1/usage", headers=_h("admin-secret")).status_code == 401  # admin is not an org
