"""
Scheduling MCP — the "shift my day" capability.

Tools:
  plan_day            — given current time + available window + fixed blocks +
                        a list of tasks, produce a time-boxed schedule using
                        learned task estimates. Does NOT commit to Notion /
                        Calendar — just returns the plan.
  record_task_duration— log an observed or stated duration for a task pattern
                        so future scheduling gets smarter.

  create_reminder     — schedule a cross-platform reminder
                        (fires as web push + optional linked Notion task)
  list_reminders      — upcoming + delivered
  cancel_reminder     — cancel a scheduled reminder
  snooze_reminder     — push a scheduled reminder forward N minutes

The reschedule solver is a greedy priority-fit that:
  1. Splits the user's available window by their fixed blocks into open slots.
  2. Sorts tasks by (explicit priority desc, due asc, p50 desc).
  3. For each task, picks the first open slot ≥ p50 and reduces the slot.
  4. Anything that doesn't fit is emitted as "deferred" with a reason.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sentrial.core import reminders as rem
from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import TaskRunner
from sentrial.evolution import task_estimates
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)


# -------------------- scheduling --------------------

def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(d: datetime) -> str:
    # Render naive-local-looking ISO without forcing UTC conversion.
    return d.isoformat(timespec="minutes")


async def plan_day(args: dict) -> Any:
    """
    Slot a list of tasks into the available window around fixed blocks.

    args:
      now:         ISO datetime (default: UTC now)
      available_from: ISO datetime — earliest start (default: now)
      available_until: ISO datetime — hard cutoff (required)
      timezone:    IANA tz string (informational; client is responsible for
                   handing us the correct local times)
      fixed_blocks: list of {start, end, label} — immovable slots
      tasks:       list of {title, pattern?, priority?, due?, minutes?}
                   - pattern drives the learned estimate; if absent, the title
                     is slugified as a fallback pattern.
                   - priority: 1 (highest) .. 5 (lowest), default 3
                   - minutes: if explicit, overrides the learned estimate.
      risk:        "p50" (default) or "p90" — p90 leaves buffer.

    Returns: {scheduled: [...], deferred: [...], leftover_minutes}
    """
    available_from = args.get("available_from") or args.get("now")
    available_until = args.get("available_until")
    if not available_from or not available_until:
        return {"error": "available_from + available_until required"}
    try:
        win_start = _parse_dt(available_from)
        win_end = _parse_dt(available_until)
    except ValueError as e:
        return {"error": f"bad datetime: {e}"}

    fixed_blocks_in = args.get("fixed_blocks") or []
    blocks: list[tuple[datetime, datetime, str]] = []
    for b in fixed_blocks_in:
        try:
            blocks.append((
                _parse_dt(b["start"]), _parse_dt(b["end"]),
                str(b.get("label") or "fixed"),
            ))
        except (KeyError, ValueError):
            continue
    blocks.sort()

    # Build open slots = win_start..win_end minus every block.
    slots: list[list] = []
    cursor = win_start
    for bs, be, _ in blocks:
        if be <= cursor:
            continue
        if bs > cursor:
            slots.append([cursor, min(bs, win_end)])
        cursor = max(cursor, be)
        if cursor >= win_end:
            break
    if cursor < win_end:
        slots.append([cursor, win_end])
    # Filter out zero/negative slots.
    slots = [s for s in slots if (s[1] - s[0]).total_seconds() > 0]

    risk = args.get("risk") or "p50"
    tasks_in = args.get("tasks") or []
    # Sort by priority asc (1 = top), then earliest due, then longest p50
    # (so big things get placed first while the day is empty).
    def _prio_key(t: dict) -> tuple:
        prio = int(t.get("priority") or 3)
        due_s = t.get("due") or "9999-12-31"
        try:
            due_d = _parse_dt(due_s if "T" in due_s else due_s + "T23:59:00Z")
        except ValueError:
            due_d = datetime(9999, 12, 31, tzinfo=timezone.utc)
        pattern = t.get("pattern") or _pattern_from_title(t.get("title", ""))
        est = int(t.get("minutes") or (
            task_estimates.p50(pattern) if risk == "p50"
            else task_estimates.p90(pattern)
        ))
        return (prio, due_d, -est)

    tasks_sorted = sorted(tasks_in, key=_prio_key)

    scheduled: list[dict] = []
    deferred: list[dict] = []

    for task in tasks_sorted:
        title = str(task.get("title") or "").strip()
        if not title:
            continue
        pattern = task.get("pattern") or _pattern_from_title(title)
        est = int(task.get("minutes") or (
            task_estimates.p50(pattern) if risk == "p50"
            else task_estimates.p90(pattern)
        ))
        placed = False
        for slot in slots:
            slot_minutes = (slot[1] - slot[0]).total_seconds() / 60
            if est <= slot_minutes:
                start = slot[0]
                end = start + timedelta(minutes=est)
                scheduled.append({
                    "title": title,
                    "pattern": pattern,
                    "start": _iso(start),
                    "end": _iso(end),
                    "estimate_minutes": est,
                    "estimate_confidence": task_estimates.get(pattern).get("confidence"),
                    "risk": risk,
                })
                slot[0] = end
                placed = True
                break
        if not placed:
            deferred.append({
                "title": title,
                "pattern": pattern,
                "estimate_minutes": est,
                "reason": "no remaining slot large enough",
            })

    leftover_minutes = int(sum((s[1] - s[0]).total_seconds() for s in slots) / 60)
    return {
        "window": {"from": _iso(win_start), "until": _iso(win_end)},
        "fixed_blocks": [
            {"start": _iso(bs), "end": _iso(be), "label": lbl}
            for bs, be, lbl in blocks
        ],
        "scheduled": scheduled,
        "deferred": deferred,
        "leftover_minutes": leftover_minutes,
        "risk": risk,
    }


def _pattern_from_title(title: str) -> str:
    t = (title or "").lower()
    # Keyword-based shortcuts so the solver uses known cold-start estimates
    # even on first run.
    if "proposal" in t:       return "proposal"
    if "audit" in t:          return "audit"
    if "demo" in t or "prototype" in t: return "demo"
    if "follow" in t:         return "followup_email"
    if "cold" in t and "email" in t: return "cold_outreach"
    if "meeting" in t or "call" in t: return "meeting"
    if "1:1" in t or "1on1" in t or "sync with" in t: return "1on1"
    if "plan" in t or "brief" in t: return "daily_brief"
    if "notion" in t:         return "notion_update"
    return "default"


async def record_task_duration(args: dict) -> Any:
    """
    Log an observed duration so future scheduling learns. args:
      pattern: str (optional; derived from title if missing)
      title:   str (optional — used to derive pattern if missing)
      minutes: number
      source:  "agent" | "user" | "observed"
    """
    minutes = args.get("minutes")
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return {"error": "positive minutes required"}
    pattern = args.get("pattern") or _pattern_from_title(args.get("title", ""))
    doc = task_estimates.record(pattern, float(minutes), source=args.get("source", "agent"))
    return doc


# -------------------- reminders --------------------

async def create_reminder_tool(args: dict) -> Any:
    title = args.get("title")
    due_at = args.get("due_at")
    if not title or not due_at:
        return {"error": "title + due_at required"}
    channels = args.get("channels") or ["push"]
    try:
        return rem.create(
            title=title, due_at=due_at, body=args.get("body", ""),
            channels=channels, source="agent",
            notion_task_id=args.get("notion_task_id"),
        )
    except ValueError as e:
        return {"error": str(e)}


async def list_reminders_tool(args: dict) -> Any:
    status = args.get("status")
    if status == "upcoming" or not status:
        return {"reminders": rem.list_upcoming(limit=int(args.get("limit", 50)))}
    return {"reminders": rem.list_all(status=status, limit=int(args.get("limit", 50)))}


async def cancel_reminder_tool(args: dict) -> Any:
    rid = args.get("reminder_id")
    if not rid:
        return {"error": "reminder_id required"}
    ok = rem.cancel(rid)
    return {"ok": ok}


async def snooze_reminder_tool(args: dict) -> Any:
    rid = args.get("reminder_id")
    minutes = args.get("minutes")
    if not rid or not isinstance(minutes, (int, float)):
        return {"error": "reminder_id + minutes required"}
    r = rem.snooze(rid, int(minutes))
    if not r:
        return {"error": "not found or not scheduled"}
    return r


# -------------------- tool registration --------------------

TOOLS = [
    Tool(
        name="plan_day",
        description=(
            "Produce a time-boxed plan for the remaining day given the user's "
            "available window, their fixed immovable blocks (meals, meetings, "
            "do-not-disturb ranges), and a list of tasks to schedule. Uses "
            "learned per-task duration estimates. Returns {scheduled, deferred, "
            "leftover_minutes} — the agent presents this plan to the user for "
            "approval before committing to Notion/Calendar."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "available_from": {"type": "string", "description": "ISO datetime; when the user is available to start."},
                "available_until": {"type": "string", "description": "ISO datetime; hard cutoff."},
                "fixed_blocks": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "label": {"type": "string"},
                    }, "required": ["start", "end"]},
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "title": {"type": "string"},
                        "pattern": {"type": "string", "description": "Optional; task kind slug for learned estimate"},
                        "priority": {"type": "integer", "description": "1 highest .. 5 lowest"},
                        "due": {"type": "string"},
                        "minutes": {"type": "integer", "description": "Overrides learned estimate"},
                    }, "required": ["title"]},
                },
                "risk": {"type": "string", "enum": ["p50", "p90"], "default": "p50"},
                "timezone": {"type": "string"},
            },
            "required": ["available_until", "tasks"],
        },
        impl=plan_day,
        tier=Tier.READ,  # planning is read-only; committing is a separate call.
    ),
    Tool(
        name="record_task_duration",
        description=(
            "Record how long a task actually took so future plan_day calls get "
            "more accurate. Call this when the user says 'that took me 2 hours' "
            "or when a completed task has observed start/end times."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "title": {"type": "string"},
                "minutes": {"type": "number"},
                "source": {"type": "string"},
            },
            "required": ["minutes"],
        },
        impl=record_task_duration,
        tier=Tier.DRAFT,
    ),
    Tool(
        name="create_cross_platform_reminder",
        description=(
            "Schedule a cross-platform reminder that fires as a web-push "
            "notification on the user's phone (and optionally syncs to a "
            "Notion task). Use for time-based nudges — unlike Apple "
            "Reminders, this works from anywhere Sentrial runs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due_at": {"type": "string", "description": "ISO datetime"},
                "body": {"type": "string"},
                "channels": {"type": "array", "items": {"type": "string", "enum": ["push", "email", "notion"]}},
                "notion_task_id": {"type": "string", "description": "Optional Notion page id to auto-cancel if task is marked Done"},
            },
            "required": ["title", "due_at"],
        },
        impl=create_reminder_tool,
        tier=Tier.SEND,
    ),
    Tool(
        name="list_cross_platform_reminders",
        description="List upcoming or delivered cross-platform reminders.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["upcoming", "scheduled", "delivered", "cancelled"]},
                "limit": {"type": "integer", "default": 50},
            },
        },
        impl=list_reminders_tool,
        tier=Tier.READ,
    ),
    Tool(
        name="cancel_cross_platform_reminder",
        description="Cancel a scheduled cross-platform reminder.",
        input_schema={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
        impl=cancel_reminder_tool,
        tier=Tier.SEND,
    ),
    Tool(
        name="snooze_cross_platform_reminder",
        description="Push a scheduled reminder forward by N minutes.",
        input_schema={
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string"},
                "minutes": {"type": "integer"},
            },
            "required": ["reminder_id", "minutes"],
        },
        impl=snooze_reminder_tool,
        tier=Tier.SEND,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    for t in TOOLS:
        registry.add(t)
