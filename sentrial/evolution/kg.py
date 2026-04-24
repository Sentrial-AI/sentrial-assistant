"""
Semantic knowledge graph — people, projects, clients, deals, and their edges.

Backed by SQLite at /data/evolution/kg.sqlite. Every write is audited. Reads
are cheap and bounded.

Schema is intentionally open-ended (`attrs_json` dict on both nodes and edges)
so evolution can add new attributes without migrations. Canonicalization is
handled via an aliases table: multiple names can point at one entity.

Retrieval API:
  - lookup(name) → entity row or None, resolving aliases
  - mention_index(text) → set of entity_ids whose name/alias appears in text
  - card(entity_id) → compact text blob the agent can read pre-turn
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_entities (
    id           TEXT    PRIMARY KEY,
    type         TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    attrs_json   TEXT    NOT NULL DEFAULT '{}',
    confidence   REAL    NOT NULL DEFAULT 0.5,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS kg_entities_type ON kg_entities(type);
CREATE INDEX IF NOT EXISTS kg_entities_name ON kg_entities(name);

CREATE TABLE IF NOT EXISTS kg_aliases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id  TEXT    NOT NULL,
    alias      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE(alias),
    FOREIGN KEY(entity_id) REFERENCES kg_entities(id)
);

CREATE TABLE IF NOT EXISTS kg_edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT    NOT NULL,
    target_id    TEXT    NOT NULL,
    relation     TEXT    NOT NULL,
    attrs_json   TEXT    NOT NULL DEFAULT '{}',
    confidence   REAL    NOT NULL DEFAULT 0.5,
    created_at   TEXT    NOT NULL,
    UNIQUE(source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS kg_edges_src ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS kg_edges_tgt ON kg_edges(target_id);

CREATE TABLE IF NOT EXISTS kg_mentions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id    TEXT    NOT NULL,
    conversation_id TEXT,
    at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS kg_mentions_entity ON kg_mentions(entity_id);
CREATE INDEX IF NOT EXISTS kg_mentions_at ON kg_mentions(at);
"""


def _db_path() -> Path:
    p = paths.data_dir() / "evolution"
    p.mkdir(parents=True, exist_ok=True)
    return p / "kg.sqlite"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path())
    con.executescript(_SCHEMA)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- CRUD ----

def upsert_entity(
    etype: str,
    name: str,
    attrs: dict | None = None,
    confidence: float = 0.5,
    aliases: list[str] | None = None,
) -> str:
    """Insert or update an entity by (type, name). Returns its id."""
    con = _conn()
    try:
        row = con.execute(
            "SELECT id, attrs_json, confidence FROM kg_entities WHERE type=? AND name=?",
            (etype, name),
        ).fetchone()
        if row:
            eid = row[0]
            merged = json.loads(row[1] or "{}")
            merged.update(attrs or {})
            new_conf = min(1.0, max(float(row[2]), float(confidence)))
            con.execute(
                "UPDATE kg_entities SET attrs_json=?, confidence=?, updated_at=? WHERE id=?",
                (json.dumps(merged, default=str), new_conf, _now(), eid),
            )
        else:
            eid = f"ent_{uuid.uuid4().hex[:10]}"
            con.execute(
                "INSERT INTO kg_entities (id, type, name, attrs_json, confidence, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (eid, etype, name, json.dumps(attrs or {}, default=str),
                 confidence, _now(), _now()),
            )
        # Aliases — ignore conflicts (an alias already pointing elsewhere stays).
        for a in (aliases or []):
            try:
                con.execute(
                    "INSERT INTO kg_aliases (entity_id, alias, created_at) VALUES (?,?,?)",
                    (eid, a.strip(), _now()),
                )
            except sqlite3.IntegrityError:
                pass
        con.commit()
    finally:
        con.close()
    audit.log("sentrial", "kg_entity_upsert", 1, args={"id": eid, "type": etype, "name": name})
    return eid


def upsert_edge(
    source_id: str, target_id: str, relation: str,
    attrs: dict | None = None, confidence: float = 0.5,
) -> int:
    con = _conn()
    try:
        row = con.execute(
            "SELECT id FROM kg_edges WHERE source_id=? AND target_id=? AND relation=?",
            (source_id, target_id, relation),
        ).fetchone()
        if row:
            con.execute(
                "UPDATE kg_edges SET attrs_json=?, confidence=? WHERE id=?",
                (json.dumps(attrs or {}, default=str),
                 min(1.0, max(0.0, float(confidence))), row[0]),
            )
            eid = row[0]
        else:
            cur = con.execute(
                "INSERT INTO kg_edges (source_id, target_id, relation, attrs_json, confidence, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (source_id, target_id, relation,
                 json.dumps(attrs or {}, default=str),
                 min(1.0, max(0.0, float(confidence))), _now()),
            )
            eid = cur.lastrowid
        con.commit()
        return eid or 0
    finally:
        con.close()


def lookup(name_or_alias: str) -> dict | None:
    """Resolve a name or alias to an entity row. Case-insensitive."""
    con = _conn()
    try:
        q = name_or_alias.strip()
        if not q:
            return None
        # Direct name match first.
        row = con.execute(
            "SELECT id,type,name,attrs_json,confidence,created_at,updated_at"
            " FROM kg_entities WHERE LOWER(name)=LOWER(?)",
            (q,),
        ).fetchone()
        if not row:
            # Try alias.
            alias_row = con.execute(
                "SELECT entity_id FROM kg_aliases WHERE LOWER(alias)=LOWER(?)", (q,),
            ).fetchone()
            if not alias_row:
                return None
            row = con.execute(
                "SELECT id,type,name,attrs_json,confidence,created_at,updated_at"
                " FROM kg_entities WHERE id=?",
                (alias_row[0],),
            ).fetchone()
            if not row:
                return None
        return _entity_row(row)
    finally:
        con.close()


def _entity_row(row: tuple) -> dict:
    return {
        "id": row[0], "type": row[1], "name": row[2],
        "attrs": json.loads(row[3] or "{}"),
        "confidence": row[4],
        "created_at": row[5], "updated_at": row[6],
    }


def list_entities(etype: str | None = None, limit: int = 200) -> list[dict]:
    con = _conn()
    try:
        if etype:
            rows = con.execute(
                "SELECT id,type,name,attrs_json,confidence,created_at,updated_at"
                " FROM kg_entities WHERE type=? ORDER BY updated_at DESC LIMIT ?",
                (etype, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id,type,name,attrs_json,confidence,created_at,updated_at"
                " FROM kg_entities ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_entity_row(r) for r in rows]
    finally:
        con.close()


def edges_from(entity_id: str) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT target_id,relation,attrs_json,confidence FROM kg_edges WHERE source_id=?",
            (entity_id,),
        ).fetchall()
        return [
            {"target_id": r[0], "relation": r[1],
             "attrs": json.loads(r[2] or "{}"), "confidence": r[3]}
            for r in rows
        ]
    finally:
        con.close()


# ---- mention extraction + retrieval ----

def mention_index(text: str) -> list[dict]:
    """
    Return distinct entities whose name or alias appears in `text`. Case
    insensitive. Excellent for pre-turn context injection.
    """
    if not text:
        return []
    con = _conn()
    try:
        names = con.execute("SELECT id, name FROM kg_entities").fetchall()
        aliases = con.execute("SELECT entity_id, alias FROM kg_aliases").fetchall()
    finally:
        con.close()
    lower = text.lower()
    hits: dict[str, float] = {}
    for eid, nm in names:
        if nm and len(nm) >= 3 and re.search(r"\b" + re.escape(nm.lower()) + r"\b", lower):
            hits[eid] = max(hits.get(eid, 0), 1.0)
    for eid, al in aliases:
        if al and len(al) >= 2 and re.search(r"\b" + re.escape(al.lower()) + r"\b", lower):
            hits[eid] = max(hits.get(eid, 0), 0.9)
    out: list[dict] = []
    for eid in hits:
        row = _fetch_entity(eid)
        if row:
            out.append(row)
    return out


def _fetch_entity(eid: str) -> dict | None:
    con = _conn()
    try:
        row = con.execute(
            "SELECT id,type,name,attrs_json,confidence,created_at,updated_at"
            " FROM kg_entities WHERE id=?", (eid,),
        ).fetchone()
        return _entity_row(row) if row else None
    finally:
        con.close()


def record_mention(entity_id: str, conversation_id: str | None = None) -> None:
    con = _conn()
    try:
        con.execute(
            "INSERT INTO kg_mentions (entity_id, conversation_id, at) VALUES (?,?,?)",
            (entity_id, conversation_id, _now()),
        )
        con.commit()
    finally:
        con.close()


def card(entity_id: str) -> str:
    """A compact multi-line card for the agent. Returns '' if entity missing."""
    ent = _fetch_entity(entity_id)
    if not ent:
        return ""
    lines = [f"{ent['type']}: {ent['name']}"]
    attrs = ent.get("attrs") or {}
    for k, v in attrs.items():
        if v is None or v == "":
            continue
        sv = v if isinstance(v, str) else json.dumps(v, default=str)
        lines.append(f"  {k}: {sv[:140]}")
    edges = edges_from(entity_id)[:6]
    for e in edges:
        tgt = _fetch_entity(e["target_id"])
        if tgt:
            lines.append(f"  —{e['relation']}→ {tgt['type']}:{tgt['name']}")
    return "\n".join(lines)


def cards_for_text(text: str, max_cards: int = 4) -> str:
    """Resolve entities mentioned in text → multi-entity card block."""
    ents = mention_index(text)[:max_cards]
    if not ents:
        return ""
    cards = [card(e["id"]) for e in ents]
    cards = [c for c in cards if c]
    if not cards:
        return ""
    return "[active context]\n" + "\n\n".join(cards)
