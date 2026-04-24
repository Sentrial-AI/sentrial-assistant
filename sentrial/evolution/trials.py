"""
A/B trials + outcome tracking.

A *trial* is a scoped experiment that applies a candidate surface change to
a percentage of matching turns, keeps the rest on baseline, and aggregates
per-variant metrics. This is riskier than replay eval but produces real
signal from real use — we only enable trials once replay delta is already
positive, as a confidence booster before merging.

Persisted at /data/evolution/trials.sqlite:
  - trials:   trial meta + target + variants
  - variants: one row per variant (baseline is implicit)
  - assigns:  conversation_id → trial_id → variant (sticky assignment)
  - outcomes: per-turn metric snapshot tagged by (trial_id, variant)

Integrity requirements:
  - trials expire automatically after `max_duration_h`
  - a trial can be stopped early if its regression rule fires
  - all assignment is deterministic via hash(conversation_id + trial_id) %
    100, so the same conversation stays on the same variant (no flip-flopping)
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    target          TEXT NOT NULL,          -- e.g. 'system_prompt', 'lesson:lsn_abc'
    baseline_sha    TEXT NOT NULL,
    variant_sha     TEXT NOT NULL,
    variant_body    TEXT NOT NULL,
    baseline_body   TEXT NOT NULL,
    treatment_pct   INTEGER NOT NULL,       -- 0..100
    status          TEXT NOT NULL DEFAULT 'running',  -- running | stopped | completed
    started_at      TEXT NOT NULL,
    ends_at         TEXT NOT NULL,
    stop_reason     TEXT
);

CREATE TABLE IF NOT EXISTS trial_assigns (
    trial_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    variant         TEXT NOT NULL,           -- 'baseline' | 'treatment'
    assigned_at     TEXT NOT NULL,
    PRIMARY KEY(trial_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS trial_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id        TEXT NOT NULL,
    variant         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    metric          TEXT NOT NULL,
    value           REAL NOT NULL,
    at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trial_outcomes_trial ON trial_outcomes(trial_id, variant, metric);
"""


def _db_path() -> Path:
    p = paths.data_dir() / "evolution"
    p.mkdir(parents=True, exist_ok=True)
    return p / "trials.sqlite"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path())
    con.executescript(_SCHEMA)
    return con


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat()


# ---- CRUD ----

def start_trial(
    name: str,
    target: str,
    baseline_body: str,
    variant_body: str,
    treatment_pct: int = 25,
    max_duration_h: int = 168,
) -> dict:
    if not (0 < treatment_pct < 100):
        raise ValueError("treatment_pct must be in (0, 100)")
    if baseline_body == variant_body:
        raise ValueError("baseline and variant are identical")
    tid = f"tri_{uuid.uuid4().hex[:10]}"
    base_sha = hashlib.sha1(baseline_body.encode()).hexdigest()[:12]
    var_sha = hashlib.sha1(variant_body.encode()).hexdigest()[:12]
    now = _now()
    ends = now + timedelta(hours=max_duration_h)
    con = _conn()
    try:
        con.execute(
            "INSERT INTO trials (id, name, target, baseline_sha, variant_sha,"
            " variant_body, baseline_body, treatment_pct, status,"
            " started_at, ends_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, name, target, base_sha, var_sha, variant_body, baseline_body,
             treatment_pct, "running", _iso(now), _iso(ends)),
        )
        con.commit()
    finally:
        con.close()
    audit.log(
        "sentrial", "trial_started", 2,
        args={"id": tid, "target": target, "pct": treatment_pct},
        result=name[:200],
    )
    return {"id": tid, "name": name, "target": target,
            "treatment_pct": treatment_pct, "ends_at": _iso(ends)}


def stop_trial(trial_id: str, reason: str = "manual") -> bool:
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE trials SET status='stopped', stop_reason=? WHERE id=? AND status='running'",
            (reason, trial_id),
        )
        con.commit()
        changed = cur.rowcount > 0
    finally:
        con.close()
    if changed:
        audit.log("user", "trial_stopped", 2, args={"id": trial_id}, result=reason[:200])
    return changed


def list_active() -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT id, name, target, treatment_pct, started_at, ends_at, status"
            " FROM trials WHERE status='running'"
        ).fetchall()
        return [
            {"id": r[0], "name": r[1], "target": r[2], "treatment_pct": r[3],
             "started_at": r[4], "ends_at": r[5], "status": r[6]}
            for r in rows
        ]
    finally:
        con.close()


def list_all(limit: int = 100) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT id, name, target, treatment_pct, started_at, ends_at, status, stop_reason"
            " FROM trials ORDER BY started_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [
            {"id": r[0], "name": r[1], "target": r[2], "treatment_pct": r[3],
             "started_at": r[4], "ends_at": r[5], "status": r[6],
             "stop_reason": r[7]}
            for r in rows
        ]
    finally:
        con.close()


# ---- assignment + surface selection ----

def assign(trial_id: str, conversation_id: str) -> str:
    """Return 'treatment' or 'baseline'. Deterministic + sticky."""
    con = _conn()
    try:
        row = con.execute(
            "SELECT variant FROM trial_assigns WHERE trial_id=? AND conversation_id=?",
            (trial_id, conversation_id),
        ).fetchone()
        if row:
            return row[0]
        trow = con.execute(
            "SELECT treatment_pct, status FROM trials WHERE id=?", (trial_id,),
        ).fetchone()
        if not trow or trow[1] != "running":
            return "baseline"
        pct = int(trow[0])
        h = hashlib.sha1(f"{trial_id}:{conversation_id}".encode()).digest()
        bucket = h[0] % 100   # 0..99
        variant = "treatment" if bucket < pct else "baseline"
        con.execute(
            "INSERT INTO trial_assigns (trial_id, conversation_id, variant, assigned_at)"
            " VALUES (?,?,?,?)",
            (trial_id, conversation_id, variant, _iso(_now())),
        )
        con.commit()
        return variant
    finally:
        con.close()


def resolve_surface(
    target: str, conversation_id: str, baseline: str,
) -> tuple[str, str | None, str]:
    """
    Given the target's baseline content, return (effective_content, trial_id,
    variant). If no running trial matches `target`, returns (baseline, None,
    'baseline').
    """
    con = _conn()
    try:
        row = con.execute(
            "SELECT id, variant_body FROM trials WHERE target=? AND status='running'"
            " ORDER BY started_at DESC LIMIT 1",
            (target,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return baseline, None, "baseline"
    tid, variant_body = row[0], row[1]
    variant = assign(tid, conversation_id)
    return (variant_body if variant == "treatment" else baseline), tid, variant


# ---- outcome recording ----

def record_outcome(
    trial_id: str, conversation_id: str, metric: str, value: float,
) -> None:
    con = _conn()
    try:
        row = con.execute(
            "SELECT variant FROM trial_assigns WHERE trial_id=? AND conversation_id=?",
            (trial_id, conversation_id),
        ).fetchone()
        variant = row[0] if row else "baseline"
        con.execute(
            "INSERT INTO trial_outcomes (trial_id, variant, conversation_id, metric, value, at)"
            " VALUES (?,?,?,?,?,?)",
            (trial_id, variant, conversation_id, metric, value, _iso(_now())),
        )
        con.commit()
    finally:
        con.close()


def summarize(trial_id: str) -> dict:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT variant, metric, AVG(value), COUNT(value)"
            " FROM trial_outcomes WHERE trial_id=? GROUP BY variant, metric",
            (trial_id,),
        ).fetchall()
        meta = con.execute(
            "SELECT name, target, treatment_pct, status, started_at, ends_at, stop_reason"
            " FROM trials WHERE id=?", (trial_id,),
        ).fetchone()
    finally:
        con.close()
    groups: dict[str, dict[str, dict]] = {"baseline": {}, "treatment": {}}
    for variant, metric, avg, n in rows:
        groups.setdefault(variant, {})[metric] = {"avg": round(avg, 4), "n": n}
    return {
        "id": trial_id,
        "name": meta[0] if meta else None,
        "target": meta[1] if meta else None,
        "treatment_pct": meta[2] if meta else None,
        "status": meta[3] if meta else None,
        "started_at": meta[4] if meta else None,
        "ends_at": meta[5] if meta else None,
        "stop_reason": meta[6] if meta else None,
        "metrics": groups,
    }


# ---- housekeeping ----

def expire_due() -> int:
    now = _iso(_now())
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE trials SET status='completed' WHERE status='running' AND ends_at<=?",
            (now,),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()
