"""
Persistent memory. SQLite at paths.memory_db_path() — Mac local or Railway volume.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sentrial.core import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value_json  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE(scope, key)
);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope);

CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT    PRIMARY KEY,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT,
    channel      TEXT    NOT NULL,
    turns_json   TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS projects (
    name         TEXT    PRIMARY KEY,
    state_json   TEXT    NOT NULL DEFAULT '{}',
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint     TEXT    NOT NULL UNIQUE,
    keys_json    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(paths.memory_db_path())
    con.executescript(_SCHEMA)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def remember(scope: str, key: str, value: Any) -> None:
    con = _conn()
    now = _now()
    con.execute(
        """INSERT INTO facts (scope, key, value_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(scope, key) DO UPDATE SET
               value_json = excluded.value_json,
               updated_at = excluded.updated_at""",
        (scope, key, json.dumps(value, default=str), now, now),
    )
    con.commit()
    con.close()


def recall(scope: str, key: str) -> Any | None:
    con = _conn()
    row = con.execute(
        "SELECT value_json FROM facts WHERE scope = ? AND key = ?", (scope, key)
    ).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def recall_scope(scope: str) -> dict[str, Any]:
    con = _conn()
    rows = con.execute(
        "SELECT key, value_json FROM facts WHERE scope = ? ORDER BY key", (scope,)
    ).fetchall()
    con.close()
    return {k: json.loads(v) for k, v in rows}


def list_pins() -> list[dict]:
    """Return all facts as pin-like objects for the UI."""
    con = _conn()
    rows = con.execute(
        "SELECT scope, key, value_json, updated_at FROM facts ORDER BY updated_at DESC"
    ).fetchall()
    con.close()
    out = []
    for scope, key, val, updated in rows:
        try:
            v = json.loads(val)
            text = v if isinstance(v, str) else json.dumps(v)
        except Exception:  # noqa: BLE001
            text = val
        out.append({"scope": scope, "key": key, "text": text[:200], "updated_at": updated})
    return out


def forget(scope: str, key: str) -> bool:
    con = _conn()
    cur = con.execute("DELETE FROM facts WHERE scope = ? AND key = ?", (scope, key))
    con.commit()
    deleted = cur.rowcount > 0
    con.close()
    return deleted


def log_turn(conversation_id: str, channel: str, turn: dict) -> None:
    con = _conn()
    now = _now()
    row = con.execute(
        "SELECT turns_json FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row:
        turns = json.loads(row[0])
        turns.append(turn)
        con.execute(
            "UPDATE conversations SET turns_json = ? WHERE id = ?",
            (json.dumps(turns, default=str), conversation_id),
        )
    else:
        con.execute(
            "INSERT INTO conversations (id, started_at, channel, turns_json) VALUES (?,?,?,?)",
            (conversation_id, now, channel, json.dumps([turn], default=str)),
        )
    con.commit()
    con.close()


# ----- Web-push subscriptions -----

def save_push_subscription(endpoint: str, keys: dict) -> None:
    con = _conn()
    now = _now()
    con.execute(
        """INSERT INTO push_subscriptions (endpoint, keys_json, created_at, last_seen)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET last_seen = excluded.last_seen""",
        (endpoint, json.dumps(keys), now, now),
    )
    con.commit()
    con.close()


def list_push_subscriptions() -> list[dict]:
    con = _conn()
    rows = con.execute("SELECT endpoint, keys_json FROM push_subscriptions").fetchall()
    con.close()
    return [{"endpoint": e, "keys": json.loads(k)} for e, k in rows]


def remove_push_subscription(endpoint: str) -> None:
    con = _conn()
    con.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    con.commit()
    con.close()
