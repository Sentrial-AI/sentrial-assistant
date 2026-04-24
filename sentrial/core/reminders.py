"""
Cross-platform reminders — the thing that fires web push / system
notifications at a scheduled time. Not tied to Apple Reminders.

Stored in SQLite at /data/reminders.sqlite. A background sweeper (started
by the daemon) checks for due reminders every 30s and hands them to the
push dispatcher.

Each reminder is:
    { id, title, body, due_at (ISO UTC), channels (list: "push"|"notion"|"email"),
      source (who created it — "user" | "agent" | "distilled" | "notion_sync"),
      notion_task_id (optional linkback),
      delivered_at (null until fired),
      status (scheduled | delivered | cancelled) }

Notion sync: if `notion_task_id` is set, the sweeper polls Notion on delivery
to double-check the task hasn't been marked Done; if Done, it cancels
instead of delivering.

Web push pipeline reuses the existing push_subscriptions table + VAPID keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, memory, paths, secrets

log = logging.getLogger(__name__)

DB_PATH = lambda: paths.data_dir() / "reminders.sqlite"  # noqa: E731
SWEEP_INTERVAL_S = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id              TEXT    PRIMARY KEY,
    title           TEXT    NOT NULL,
    body            TEXT    NOT NULL DEFAULT '',
    due_at          TEXT    NOT NULL,
    channels_json   TEXT    NOT NULL DEFAULT '["push"]',
    source          TEXT    NOT NULL DEFAULT 'user',
    notion_task_id  TEXT,
    created_at      TEXT    NOT NULL,
    delivered_at    TEXT,
    status          TEXT    NOT NULL DEFAULT 'scheduled'
);
CREATE INDEX IF NOT EXISTS rem_due ON reminders(status, due_at);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH())
    con.executescript(_SCHEMA)
    return con


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat()


# ---- CRUD ----

def create(
    title: str,
    due_at: str | datetime,
    body: str = "",
    channels: list[str] | None = None,
    source: str = "user",
    notion_task_id: str | None = None,
) -> dict:
    if not title.strip():
        raise ValueError("title required")
    due = due_at if isinstance(due_at, datetime) else _parse_iso(due_at)
    rid = f"rem_{uuid.uuid4().hex[:10]}"
    doc = {
        "id": rid,
        "title": title.strip(),
        "body": body,
        "due_at": _iso(due),
        "channels": channels or ["push"],
        "source": source,
        "notion_task_id": notion_task_id,
        "created_at": _iso(_now()),
        "delivered_at": None,
        "status": "scheduled",
    }
    con = _conn()
    try:
        con.execute(
            "INSERT INTO reminders (id, title, body, due_at, channels_json,"
            " source, notion_task_id, created_at, delivered_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc["id"], doc["title"], doc["body"], doc["due_at"],
             json.dumps(doc["channels"]), doc["source"],
             doc["notion_task_id"], doc["created_at"],
             None, doc["status"]),
        )
        con.commit()
    finally:
        con.close()
    audit.log("user" if source == "user" else "sentrial", "reminder_create", 1,
              args={"id": rid, "due_at": doc["due_at"], "source": source},
              result=title[:200])
    return doc


def _parse_iso(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"bad due_at {s!r}: {e}")


def get(rid: str) -> dict | None:
    con = _conn()
    try:
        row = con.execute(
            "SELECT id,title,body,due_at,channels_json,source,notion_task_id,"
            "created_at,delivered_at,status FROM reminders WHERE id=?", (rid,),
        ).fetchone()
    finally:
        con.close()
    return _row(row) if row else None


def _row(r: tuple) -> dict:
    return {
        "id": r[0], "title": r[1], "body": r[2], "due_at": r[3],
        "channels": json.loads(r[4] or "[]"),
        "source": r[5], "notion_task_id": r[6],
        "created_at": r[7], "delivered_at": r[8], "status": r[9],
    }


def list_upcoming(limit: int = 50) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT id,title,body,due_at,channels_json,source,notion_task_id,"
            "created_at,delivered_at,status FROM reminders"
            " WHERE status='scheduled' ORDER BY due_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [_row(r) for r in rows]


def list_all(status: str | None = None, limit: int = 200) -> list[dict]:
    con = _conn()
    try:
        if status:
            rows = con.execute(
                "SELECT id,title,body,due_at,channels_json,source,notion_task_id,"
                "created_at,delivered_at,status FROM reminders WHERE status=?"
                " ORDER BY due_at DESC LIMIT ?", (status, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id,title,body,due_at,channels_json,source,notion_task_id,"
                "created_at,delivered_at,status FROM reminders"
                " ORDER BY due_at DESC LIMIT ?", (limit,),
            ).fetchall()
    finally:
        con.close()
    return [_row(r) for r in rows]


def cancel(rid: str) -> bool:
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE reminders SET status='cancelled' WHERE id=? AND status='scheduled'",
            (rid,),
        )
        con.commit()
        changed = cur.rowcount > 0
    finally:
        con.close()
    if changed:
        audit.log("user", "reminder_cancel", 1, args={"id": rid})
    return changed


def snooze(rid: str, minutes: int) -> dict | None:
    r = get(rid)
    if not r or r["status"] != "scheduled":
        return None
    due = _parse_iso(r["due_at"]) + timedelta(minutes=minutes)
    con = _conn()
    try:
        con.execute("UPDATE reminders SET due_at=? WHERE id=?", (_iso(due), rid))
        con.commit()
    finally:
        con.close()
    audit.log("user", "reminder_snooze", 1, args={"id": rid, "minutes": minutes})
    return get(rid)


# ---- delivery ----

async def _deliver(reminder: dict) -> bool:
    """Fire a reminder via its channels. Returns True if at least one succeeded."""
    ok_any = False
    for channel in reminder.get("channels") or ["push"]:
        try:
            if channel == "push":
                ok_any |= await _push(reminder)
            elif channel == "email":
                # Email delivery is stubbed until Gmail OAuth is wired; log + skip.
                log.info("reminder email channel not yet wired: %s", reminder["id"])
            elif channel == "notion":
                # Notion "delivery" = ping the linked task; most workflows
                # don't need this because Notion already shows the task.
                pass
        except Exception as e:  # noqa: BLE001
            log.warning("reminder deliver %s/%s failed: %s", reminder["id"], channel, e)
    return ok_any


async def _push(reminder: dict) -> bool:
    """Send a Web Push notification to every subscribed endpoint."""
    subs = memory.list_push_subscriptions()
    if not subs:
        return False
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush not installed — add to requirements.txt")
        return False
    vapid_priv = secrets.get("vapid_private_key")
    vapid_claims_email = secrets.get("vapid_claims_email") or "mailto:admin@example.com"
    if not vapid_priv:
        log.warning("VAPID_PRIVATE_KEY not set — cannot send web push")
        return False
    payload = json.dumps({
        "title": reminder["title"],
        "body": reminder.get("body") or "",
        "tag": f"reminder-{reminder['id']}",
        "url": "/ui/#reminders",
    })
    ok_any = False
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=payload,
                vapid_private_key=vapid_priv,
                vapid_claims={"sub": vapid_claims_email},
                timeout=10,
            )
            ok_any = True
        except WebPushException as e:  # noqa: F821
            # Common: 410 Gone → the sub is dead. Prune silently.
            status = getattr(e.response, "status_code", 0) if getattr(e, "response", None) else 0
            if status == 410:
                memory.remove_push_subscription(sub["endpoint"])
            else:
                log.warning("webpush failed status=%s: %s", status, e)
        except Exception as e:  # noqa: BLE001
            log.warning("webpush error: %s", e)
    return ok_any


async def _mark_delivered(rid: str) -> None:
    con = _conn()
    try:
        con.execute(
            "UPDATE reminders SET status='delivered', delivered_at=? WHERE id=? AND status='scheduled'",
            (_iso(_now()), rid),
        )
        con.commit()
    finally:
        con.close()


# ---- sweeper (scheduled background task) ----

async def _sweep_once() -> int:
    """Deliver all reminders whose due_at has passed. Returns count delivered."""
    now = _iso(_now())
    con = _conn()
    try:
        rows = con.execute(
            "SELECT id,title,body,due_at,channels_json,source,notion_task_id,"
            "created_at,delivered_at,status FROM reminders"
            " WHERE status='scheduled' AND due_at<=? ORDER BY due_at ASC",
            (now,),
        ).fetchall()
    finally:
        con.close()

    count = 0
    for r in rows:
        reminder = _row(r)
        # If linked to a Notion task that's already Done, auto-cancel.
        if reminder.get("notion_task_id"):
            try:
                done = await _notion_task_done(reminder["notion_task_id"])
                if done:
                    cancel(reminder["id"])
                    continue
            except Exception:  # noqa: BLE001
                pass
        ok = await _deliver(reminder)
        if ok:
            await _mark_delivered(reminder["id"])
            audit.log("sentrial", "reminder_delivered", 1,
                      args={"id": reminder["id"]},
                      result=reminder["title"][:200])
            count += 1
    return count


async def _notion_task_done(notion_task_id: str) -> bool:
    """Best-effort check whether a linked Notion task is Done. Fail-closed = False."""
    try:
        from sentrial.mcps.notion.server import _request
        data = await _request("GET", f"/pages/{notion_task_id}")
        for v in (data.get("properties") or {}).values():
            if v.get("type") == "status":
                return ((v.get("status") or {}).get("name") or "").lower() == "done"
    except Exception:  # noqa: BLE001
        return False
    return False


async def run_sweeper_forever() -> None:
    """Long-lived task. The daemon starts this alongside the FastAPI server."""
    log.info("reminders sweeper starting — interval=%ss", SWEEP_INTERVAL_S)
    while True:
        try:
            n = await _sweep_once()
            if n:
                log.info("reminders: delivered %d", n)
        except Exception as e:  # noqa: BLE001
            log.exception("reminders sweep error: %s", e)
        await asyncio.sleep(SWEEP_INTERVAL_S)
