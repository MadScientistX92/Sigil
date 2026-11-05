"""Provenance registry and audit log (SQLite). Doubles as the compliance evidence store."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS marks (
  content_id TEXT PRIMARY KEY, org TEXT, modality TEXT, sha256 TEXT, fingerprint TEXT,
  provider TEXT, system TEXT, system_version TEXT, created_at TEXT,
  jurisdictions TEXT, controls TEXT, ruleset TEXT
);
CREATE INDEX IF NOT EXISTS marks_sha ON marks(sha256);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, org TEXT, event TEXT, content_id TEXT, detail TEXT
);
"""

N_HASHES = 64


def _shingles(text: str, k: int = 5) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def text_fingerprint(text: str) -> list[int]:
    """MinHash signature: lets us find a logged text even after light edits."""
    sh = _shingles(text)
    if not sh:
        return []
    sig = []
    for i in range(N_HASHES):
        salt = i.to_bytes(2, "big")
        sig.append(min(int.from_bytes(hashlib.blake2b(salt + s.encode(), digest_size=8).digest(), "big")
                       for s in sh))
    return sig


def similarity(a: list[int], b: list[int]) -> float:
    if not a or not b:
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


class Registry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def record(self, *, content_id, org, modality, sha256, provider, system, system_version,
               created_at, jurisdictions, controls, ruleset, fingerprint=None):
        self.db.execute(
            "INSERT OR REPLACE INTO marks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (content_id, org, modality, sha256, json.dumps(fingerprint) if fingerprint else None,
             provider, system, system_version, created_at, json.dumps(list(jurisdictions)),
             json.dumps(controls), ruleset))
        self.audit(org, "mark", content_id, {"modality": modality, "controls": controls})
        self.db.commit()

    def audit(self, org, event, content_id=None, detail=None):
        self.db.execute("INSERT INTO audit (ts, org, event, content_id, detail) VALUES (?,?,?,?,?)",
                        (time.time(), org, event, content_id, json.dumps(detail or {})))
        self.db.commit()

    def get(self, content_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM marks WHERE content_id=?", (content_id,)).fetchone()
        return self._row(row)

    def by_hash(self, sha256: str) -> dict | None:
        row = self.db.execute("SELECT * FROM marks WHERE sha256=?", (sha256,)).fetchone()
        return self._row(row)

    def match_text(self, text: str, threshold: float = 0.5) -> tuple[dict | None, float]:
        fp = text_fingerprint(text)
        best, score = None, 0.0
        for row in self.db.execute("SELECT * FROM marks WHERE modality='text' AND fingerprint IS NOT NULL"):
            s = similarity(fp, json.loads(row["fingerprint"]))
            if s > score:
                best, score = row, s
        return (self._row(best), score) if score >= threshold else (None, score)

    def evidence(self, org: str, since: float = 0) -> dict:
        marks = [self._row(r) for r in self.db.execute(
            "SELECT * FROM marks WHERE org=? ORDER BY created_at", (org,))]
        events = [dict(r) for r in self.db.execute(
            "SELECT ts, event, content_id, detail FROM audit WHERE org=? AND ts>=? ORDER BY ts", (org, since))]
        by_mod: dict[str, int] = {}
        for m in marks:
            by_mod[m["modality"]] = by_mod.get(m["modality"], 0) + 1
        return {"org": org, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "totals": {"marked_items": len(marks), "by_modality": by_mod, "audit_events": len(events)},
                "marks": marks, "audit": events}

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in ("jurisdictions", "controls"):
            if d.get(k):
                d[k] = json.loads(d[k])
        d.pop("fingerprint", None)
        return d
