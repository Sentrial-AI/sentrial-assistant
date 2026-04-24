"""
Google Calendar MCP.

Tools:
  list_calendars         — every calendar the user can read/write
  list_upcoming_events   — next N events on a calendar (default: primary)
  get_event              — one event by id
  create_event           — new event (SEND)
  update_event           — change an event (SEND)
  delete_event           — delete an event (SEND)
  find_free_slots        — within a window, return gaps ≥ min_minutes;
                           essential for the "shift my day" flow

Same auth plumbing as Gmail: shared helper in core.google_oauth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from sentrial.core import google_oauth
from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import TaskRunner
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/calendar/v3"


async def _request(method: str, path: str, json: dict | None = None,
                    params: dict | None = None) -> dict:
    tok = await google_oauth.ensure_access_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.request(
            method, f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {tok}"},
            json=json, params=params,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"calendar {method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _compact_event(ev: dict) -> dict:
    start = (ev.get("start") or {})
    end = (ev.get("end") or {})
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "description": ev.get("description"),
        "location": ev.get("location"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": bool(start.get("date") and not start.get("dateTime")),
        "status": ev.get("status"),
        "attendees": [
            {"email": a.get("email"), "response": a.get("responseStatus"),
             "organizer": bool(a.get("organizer"))}
            for a in (ev.get("attendees") or [])
        ],
        "hangoutLink": ev.get("hangoutLink"),
        "htmlLink": ev.get("htmlLink"),
        "organizer": (ev.get("organizer") or {}).get("email"),
    }


# -------------------- tools --------------------

async def list_calendars(_args: dict) -> Any:
    try:
        data = await _request("GET", "/users/me/calendarList")
    except RuntimeError as e:
        return {"error": str(e)}
    out = [
        {"id": c.get("id"), "summary": c.get("summary"),
         "primary": bool(c.get("primary")),
         "access_role": c.get("accessRole"),
         "timezone": c.get("timeZone")}
        for c in data.get("items") or []
    ]
    return {"calendars": out, "count": len(out)}


async def list_upcoming_events(args: dict) -> Any:
    """Next N events on `calendar_id` (default 'primary'). args: limit, hours_window, calendar_id."""
    cal = args.get("calendar_id") or "primary"
    limit = int(args.get("limit", 10))
    hours = int(args.get("hours_window", 24 * 7))
    now = datetime.now(timezone.utc)
    params = {
        "timeMin": _iso(now),
        "timeMax": _iso(now + timedelta(hours=hours)),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": limit,
    }
    try:
        data = await _request("GET", f"/calendars/{cal}/events", params=params)
    except RuntimeError as e:
        return {"error": str(e)}
    items = [_compact_event(ev) for ev in data.get("items") or []]
    return {"events": items, "count": len(items), "calendar_id": cal}


async def get_event(args: dict) -> Any:
    eid = args.get("event_id")
    cal = args.get("calendar_id") or "primary"
    if not eid: return {"error": "event_id required"}
    try:
        ev = await _request("GET", f"/calendars/{cal}/events/{eid}")
    except RuntimeError as e:
        return {"error": str(e)}
    return _compact_event(ev)


async def create_event(args: dict) -> Any:
    """
    args:
      summary (required), start (ISO datetime, required), end (ISO datetime, required),
      description?, location?, attendees? (list of emails), calendar_id? (default primary),
      all_day? (bool — if true, start/end are dates)
    """
    summary = args.get("summary")
    start = args.get("start"); end = args.get("end")
    if not summary or not start or not end:
        return {"error": "summary + start + end required"}
    cal = args.get("calendar_id") or "primary"
    all_day = bool(args.get("all_day"))
    body: dict = {
        "summary": summary,
        "start": ({"date": start} if all_day else {"dateTime": start}),
        "end":   ({"date": end}   if all_day else {"dateTime": end}),
    }
    if args.get("description"): body["description"] = args["description"]
    if args.get("location"): body["location"] = args["location"]
    if args.get("attendees"):
        body["attendees"] = [{"email": e} for e in args["attendees"] if isinstance(e, str)]
    try:
        ev = await _request("POST", f"/calendars/{cal}/events", json=body)
    except RuntimeError as e:
        return {"error": str(e)}
    return _compact_event(ev)


async def update_event(args: dict) -> Any:
    eid = args.get("event_id"); cal = args.get("calendar_id") or "primary"
    if not eid: return {"error": "event_id required"}
    patch: dict = {}
    if "summary" in args: patch["summary"] = args["summary"]
    if "description" in args: patch["description"] = args["description"]
    if "location" in args: patch["location"] = args["location"]
    if "start" in args:
        patch["start"] = ({"date": args["start"]} if args.get("all_day") else {"dateTime": args["start"]})
    if "end" in args:
        patch["end"] = ({"date": args["end"]} if args.get("all_day") else {"dateTime": args["end"]})
    if "attendees" in args:
        patch["attendees"] = [{"email": e} for e in args["attendees"] if isinstance(e, str)]
    if not patch:
        return {"error": "no updatable fields in args"}
    try:
        ev = await _request("PATCH", f"/calendars/{cal}/events/{eid}", json=patch)
    except RuntimeError as e:
        return {"error": str(e)}
    return _compact_event(ev)


async def delete_event(args: dict) -> Any:
    eid = args.get("event_id"); cal = args.get("calendar_id") or "primary"
    if not eid: return {"error": "event_id required"}
    try:
        await _request("DELETE", f"/calendars/{cal}/events/{eid}")
    except RuntimeError as e:
        return {"error": str(e)}
    return {"ok": True, "id": eid}


async def find_free_slots(args: dict) -> Any:
    """
    Within [window_start, window_end], return gaps ≥ min_minutes on the
    primary calendar. Uses Google's freebusy.query endpoint so all-day and
    declined events are handled correctly.
    """
    start = args.get("window_start"); end = args.get("window_end")
    if not start or not end:
        return {"error": "window_start + window_end required"}
    min_minutes = int(args.get("min_minutes") or 15)
    cal = args.get("calendar_id") or "primary"
    try:
        busy_data = await _request(
            "POST", "/freeBusy",
            json={"timeMin": start, "timeMax": end, "items": [{"id": cal}]},
        )
    except RuntimeError as e:
        return {"error": str(e)}
    busy = (busy_data.get("calendars") or {}).get(cal, {}).get("busy") or []
    # Merge busy intervals, subtract from window.
    win_start = _parse(start); win_end = _parse(end)
    intervals = sorted([(_parse(b["start"]), _parse(b["end"])) for b in busy])
    merged: list[list[datetime]] = []
    for bs, be in intervals:
        if merged and bs <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], be)
        else:
            merged.append([bs, be])
    free: list[dict] = []
    cursor = win_start
    for bs, be in merged:
        if be <= cursor:
            continue
        if bs > cursor:
            dur = (bs - cursor).total_seconds() / 60
            if dur >= min_minutes:
                free.append({"start": _iso(cursor), "end": _iso(min(bs, win_end)),
                             "minutes": int(dur)})
        cursor = max(cursor, be)
        if cursor >= win_end: break
    if cursor < win_end:
        dur = (win_end - cursor).total_seconds() / 60
        if dur >= min_minutes:
            free.append({"start": _iso(cursor), "end": _iso(win_end), "minutes": int(dur)})
    return {"free_slots": free, "count": len(free), "calendar_id": cal,
            "min_minutes": min_minutes}


TOOLS = [
    Tool(
        name="list_calendars",
        description="List every Google calendar the user can access, with id / summary / primary flag / access role / timezone.",
        input_schema={"type": "object", "properties": {}},
        impl=list_calendars, tier=Tier.READ,
    ),
    Tool(
        name="list_upcoming_events",
        description=(
            "Upcoming events on `calendar_id` (default 'primary') within "
            "`hours_window` from now. Use limit=10 hours_window=24 for 'today's "
            "agenda', or hours_window=168 for 'this week'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "hours_window": {"type": "integer", "default": 168},
                "calendar_id": {"type": "string"},
            },
        },
        impl=list_upcoming_events, tier=Tier.READ,
    ),
    Tool(
        name="get_event",
        description="Fetch one event by id.",
        input_schema={
            "type": "object",
            "properties": {"event_id": {"type": "string"},
                           "calendar_id": {"type": "string"}},
            "required": ["event_id"],
        },
        impl=get_event, tier=Tier.READ,
    ),
    Tool(
        name="create_event",
        description=(
            "Create a new calendar event. `start` and `end` are ISO datetimes "
            "(or dates if `all_day` is true). Attendees is a list of email "
            "strings. Defaults to the primary calendar."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "calendar_id": {"type": "string"},
                "all_day": {"type": "boolean"},
            },
            "required": ["summary", "start", "end"],
        },
        impl=create_event, tier=Tier.SEND,
    ),
    Tool(
        name="update_event",
        description="Patch fields on an existing calendar event. Only included fields are updated.",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "all_day": {"type": "boolean"},
                "calendar_id": {"type": "string"},
            },
            "required": ["event_id"],
        },
        impl=update_event, tier=Tier.SEND,
    ),
    Tool(
        name="delete_event",
        description="Delete a calendar event by id.",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string"},
            },
            "required": ["event_id"],
        },
        impl=delete_event, tier=Tier.SEND,
    ),
    Tool(
        name="find_free_slots",
        description=(
            "Given a window [window_start, window_end], return gaps on the "
            "primary calendar ≥ min_minutes. Perfect for the reschedule flow: "
            "ask this first, then plan_day around the returned slots."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "window_start": {"type": "string"},
                "window_end": {"type": "string"},
                "min_minutes": {"type": "integer", "default": 15},
                "calendar_id": {"type": "string"},
            },
            "required": ["window_start", "window_end"],
        },
        impl=find_free_slots, tier=Tier.READ,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    from sentrial.core import google_oauth
    status = "active" if google_oauth.is_connected() else "pending_auth"
    registry.add_group("calendar", status=status)
    for t in TOOLS:
        registry.add(t)
