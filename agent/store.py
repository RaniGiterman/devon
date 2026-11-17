"""SQLite incident store. Every state change is a row in `transitions`, which gives
the scoreboard its timings for free."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

STATES = [
    "DETECTED", "TRIAGED", "INVESTIGATING", "FIX_PROPOSED", "AWAITING_APPROVAL",
    "APPLYING", "VERIFYING", "RESOLVED", "ROLLED_BACK", "ESCALATED", "DROPPED",
]
TERMINAL = {"RESOLVED", "ESCALATED", "DROPPED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL, namespace TEXT, workload TEXT,
  state TEXT NOT NULL, severity TEXT, title TEXT,
  created REAL, updated REAL, resolved REAL,
  triage TEXT, diagnosis TEXT, fix TEXT, gate TEXT,
  pr_url TEXT, pr_number INTEGER, merge_sha TEXT,
  approver TEXT, reject_reason TEXT, outcome TEXT,
  attempts INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, llm_ms INTEGER DEFAULT 0, llm_calls INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transitions (
  incident_id INTEGER, from_state TEXT, to_state TEXT, ts REAL, note TEXT);
CREATE TABLE IF NOT EXISTS signals (
  incident_id INTEGER, source TEXT, symptom TEXT, severity TEXT, summary TEXT, evidence TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS steps (
  incident_id INTEGER, ts REAL, kind TEXT, name TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_inc_key ON incidents(key, state);
"""

JSON_COLS = {"triage", "diagnosis", "fix", "gate"}


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            try:  # databases created before llm_calls existed
                self._db.execute("ALTER TABLE incidents ADD COLUMN llm_calls INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    def _x(self, sql: str, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # incidents
    def create(self, key: str, namespace: str, workload: str, severity: str, title: str) -> int:
        now = time.time()
        cur = self._x("INSERT INTO incidents(key,namespace,workload,state,severity,title,created,updated)"
                      " VALUES(?,?,?,?,?,?,?,?)", (key, namespace, workload, "DETECTED", severity, title, now, now))
        iid = cur.lastrowid
        self._x("INSERT INTO transitions VALUES(?,?,?,?,?)", (iid, None, "DETECTED", now, title))
        return iid

    def open_for_key(self, key: str) -> dict | None:
        ph = ",".join("?" * len(TERMINAL))
        row = self._x(f"SELECT * FROM incidents WHERE key=? AND state NOT IN ({ph}) ORDER BY id DESC LIMIT 1",
                      (key, *TERMINAL)).fetchone()
        return self._row(row)

    def last_closed_for_key(self, key: str) -> dict | None:
        ph = ",".join("?" * len(TERMINAL))
        row = self._x(f"SELECT * FROM incidents WHERE key=? AND state IN ({ph}) ORDER BY id DESC LIMIT 1",
                      (key, *TERMINAL)).fetchone()
        return self._row(row)

    def get(self, iid: int) -> dict | None:
        return self._row(self._x("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone())

    def list(self, limit: int = 50) -> list[dict]:
        rows = self._x("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def open_incidents(self) -> list[dict]:
        ph = ",".join("?" * len(TERMINAL))
        return [self._row(r) for r in self._x(f"SELECT * FROM incidents WHERE state NOT IN ({ph})", tuple(TERMINAL))]

    def update(self, iid: int, **fields) -> None:
        if not fields:
            return
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if k in JSON_COLS and v is not None else v for k, v in fields.items()]
        self._x(f"UPDATE incidents SET {cols} WHERE id=?", (*vals, iid))

    def add_usage(self, iid: int, tokens: int, ms: int, calls: int = 0) -> None:
        self._x("UPDATE incidents SET tokens=tokens+?, llm_ms=llm_ms+?, llm_calls=llm_calls+? WHERE id=?",
                (tokens, ms, calls, iid))

    def transition(self, iid: int, to_state: str, note: str = "") -> str:
        assert to_state in STATES, to_state
        cur = self.get(iid)
        now = time.time()
        extra = {"resolved": now} if to_state in TERMINAL else {}
        self.update(iid, state=to_state, **extra)
        self._x("INSERT INTO transitions VALUES(?,?,?,?,?)", (iid, cur["state"], to_state, now, note))
        return cur["state"]

    def transitions(self, iid: int) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM transitions WHERE incident_id=? ORDER BY ts", (iid,))]

    # signals & steps
    def add_signal(self, iid: int, sig) -> None:
        self._x("INSERT INTO signals VALUES(?,?,?,?,?,?,?)",
                (iid, sig.source, sig.symptom, sig.severity, sig.summary, json.dumps(sig.evidence), sig.ts))

    def signals(self, iid: int, since: float = 0) -> list[dict]:
        rows = self._x("SELECT * FROM signals WHERE incident_id=? AND ts>=? ORDER BY ts", (iid, since)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"] or "[]")
            out.append(d)
        return out

    def add_step(self, iid: int, kind: str, name: str, detail: str = "") -> None:
        self._x("INSERT INTO steps VALUES(?,?,?,?,?)", (iid, time.time(), kind, name, detail[:4000]))

    def steps(self, iid: int) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM steps WHERE incident_id=? ORDER BY ts", (iid,))]

    def prs_since(self, since: float) -> int:
        return self._x("SELECT COUNT(*) FROM incidents WHERE pr_number IS NOT NULL AND updated>=?",
                       (since,)).fetchone()[0]

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLS:
            if d.get(k):
                d[k] = json.loads(d[k])
        return d
