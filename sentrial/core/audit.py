"""
Append-only audit log. SQLite on a persistent volume.

Path is resolved via sentrial.core.paths so this works on Mac (~/Library/...) and on
Railway (/data via mounted volume) without code changes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sentrial.core import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    tier            INTEGER NOT NULL,
    args_json       TEXT    DEFAULT '{}',
    result_summary  TEXT    DEFAULT '',
    status          TEXT    NOT NULL,
    job_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_job ON audit(job_id);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(paths.audit_db_path())
    con.executescript(_SCHEMA)
    return con


def log(
    actor: str,
    action: str,
    tier: int,
    args: dict[str, Any] | None = None,
    result: str | None = None,
    status: str = "ok",
    job_id: str | None = None,
) -> None:
    try:
        con = _conn()
        con.execute(
            "INSERT INTO audit (timestamp, actor, action, tier, args_json, result_summary, status, job_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                actor,
                action,
                tier,
                json.dumps(args or {}, default=str)[:4000],
                (result or "")[:1000],
                status,
                job_id,
            ),
        )
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def tail(n: int = 50, actor: str | None = None, since: str | None = None) -> list[dict]:
    con = _conn()
    sql = (
        "SELECT id, timestamp, actor, action, tier, args_json, result_summary, status, job_id FROM audit"
    )
    where, params = [], []
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(n)
    rows = con.execute(sql, params).fetchall()
    con.close()
    cols = (
        "id", "timestamp", "actor", "action", "tier",
        "args_json", "result_summary", "status", "job_id",
    )
    return [dict(zip(cols, r)) for r in rows]


def count_today() -> int:
    con = _conn()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = con.execute(
        "SELECT COUNT(*) FROM audit WHERE timestamp LIKE ?", (f"{today}%",)
    ).fetchone()
    con.close()
    return row[0] if row else 0
