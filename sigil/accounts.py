"""API keys, usage metering and rate limiting.

Keys are stored hashed (SHA-256) in the same SQLite database as the registry. The plaintext key is
shown once, at creation. Usage is counted per org, per calendar month (UTC), per metric, which is
the shape metered billing needs later.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, key_hash TEXT UNIQUE, prefix TEXT, org TEXT, name TEXT,
  created_at REAL, last_used_at REAL, revoked_at REAL
);
CREATE INDEX IF NOT EXISTS api_keys_org ON api_keys(org);
CREATE TABLE IF NOT EXISTS usage (
  org TEXT, period TEXT, metric TEXT, count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (org, period, metric)
);
"""

KEY_PREFIX = "sk_sigil_"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def period_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(time.time() if ts is None else ts))


class Accounts:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._lock = threading.Lock()
        self.db.executescript(SCHEMA)

    # --- keys -------------------------------------------------------------------------------
    def create_key(self, org: str, name: str = "") -> dict:
        org = org.strip()
        if not org:
            raise ValueError("org is required")
        key = KEY_PREFIX + secrets.token_urlsafe(24)
        kid = "key_" + secrets.token_hex(6)
        now = time.time()
        with self._lock:
            self.db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?)",
                            (kid, _hash(key), key[:len(KEY_PREFIX) + 4], org, name, now, None, None))
            self.db.commit()
        return {"id": kid, "key": key, "org": org, "name": name, "created_at": now}

    def org_for_key(self, key: str) -> str | None:
        if not key:
            return None
        row = self.db.execute("SELECT id, org FROM api_keys WHERE key_hash=? AND revoked_at IS NULL",
                              (_hash(key),)).fetchone()
        if row is None:
            return None
        with self._lock:
            self.db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), row["id"]))
            self.db.commit()
        return row["org"]

    def list_keys(self, org: str | None = None) -> list[dict]:
        q, args = "SELECT id, prefix, org, name, created_at, last_used_at, revoked_at FROM api_keys", ()
        if org is not None:
            q, args = q + " WHERE org=?", (org,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at", args)]

    def revoke_key(self, key_id: str, org: str | None = None) -> bool:
        q, args = "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", [time.time(), key_id]
        if org is not None:
            q += " AND org=?"
            args.append(org)
        with self._lock:
            n = self.db.execute(q, args).rowcount
            self.db.commit()
        return n > 0

    # --- metering ---------------------------------------------------------------------------
    def meter(self, org: str, metric: str, n: int = 1, ts: float | None = None) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO usage (org, period, metric, count) VALUES (?,?,?,?) "
                "ON CONFLICT(org, period, metric) DO UPDATE SET count = count + excluded.count",
                (org, period_of(ts), metric, n))
            self.db.commit()

    def usage(self, org: str, period: str | None = None) -> dict:
        period = period or period_of()
        rows = self.db.execute("SELECT metric, count FROM usage WHERE org=? AND period=? ORDER BY metric",
                               (org, period)).fetchall()
        metrics = {r["metric"]: r["count"] for r in rows}
        return {"org": org, "period": period, "metrics": metrics, "total": sum(metrics.values())}


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds; 0 when allowed


class RateLimiter:
    """In-process token bucket: `per_minute` requests, refilled continuously, burst = per_minute.

    State lives in memory, so limits are per process and reset on restart.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def hit(self, bucket: str, per_minute: int) -> Decision:
        if per_minute <= 0:  # 0 disables the limit
            return Decision(True, 0, 0, 0)
        rate = per_minute / 60.0
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(bucket, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens >= 1:
                tokens -= 1
                self._buckets[bucket] = (tokens, now)
                if len(self._buckets) > 100_000:  # crude memory bound
                    self._buckets.clear()
                return Decision(True, per_minute, int(tokens), 0)
            self._buckets[bucket] = (tokens, now)
            return Decision(False, per_minute, 0, max(1, math.ceil((1 - tokens) / rate)))
