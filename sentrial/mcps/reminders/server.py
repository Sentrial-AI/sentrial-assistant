"""
Apple Reminders MCP. Native integration via osascript.

Tools:
  list_reminders(list_name?)     — read
  create_reminder(title, due?, list_name?, notes?) — send (tier 2, confirmation)
  complete_reminder(reminder_id) — send (tier 2)

No external API keys needed — uses the user's local Reminders database via AppleScript.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import TaskRunner
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)


async def _osascript(script: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def list_reminders(args: dict) -> Any:
    list_name: str | None = args.get("list_name")
    if list_name:
        script = (
            'set output to ""\n'
            'tell application "Reminders"\n'
            f'  set theList to list "{_esc(list_name)}"\n'
            '  repeat with r in (every reminder of theList whose completed is false)\n'
            '    set output to output & (name of r) & " ||| " & (id of r) & "\\n"\n'
            '  end repeat\n'
            'end tell\n'
            'return output'
        )
    else:
        script = (
            'set output to ""\n'
            'tell application "Reminders"\n'
            '  repeat with theList in lists\n'
            '    repeat with r in (every reminder of theList whose completed is false)\n'
            '      set output to output & (name of theList) & " :: " & (name of r) & " ||| " & (id of r) & "\\n"\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell\n'
            'return output'
        )
    rc, out, err = await _osascript(script)
    if rc != 0:
        return {"error": err.strip()}
    lines = [line for line in out.splitlines() if line.strip()]
    items = []
    for line in lines:
        if " ||| " in line:
            title_part, rid = line.rsplit(" ||| ", 1)
            items.append({"title": title_part.strip(), "id": rid.strip()})
    return {"reminders": items, "count": len(items)}


async def create_reminder(args: dict) -> Any:
    title = args.get("title")
    if not title:
        return {"error": "title is required"}
    due = args.get("due")               # ISO string or "tomorrow 5pm"
    list_name = args.get("list_name")
    notes = args.get("notes", "")

    parts = [
        f'set newRem to make new reminder with properties {{name:"{_esc(title)}"'
    ]
    if notes:
        parts[0] += f', body:"{_esc(notes)}"'
    parts[0] += "}"

    if list_name:
        script_head = (
            'tell application "Reminders"\n'
            f'  tell list "{_esc(list_name)}"\n'
            f'    {parts[0]}\n'
        )
        script_tail = '  end tell\nend tell\nreturn id of newRem'
    else:
        script_head = (
            'tell application "Reminders"\n'
            f'  {parts[0]}\n'
        )
        script_tail = 'end tell\nreturn id of newRem'

    script = script_head + script_tail
    rc, out, err = await _osascript(script)
    if rc != 0:
        return {"error": err.strip()}
    rid = out.strip()

    if due:
        # Best-effort — set remind_me_date via a second script.
        # Accepts natural-language; defers parsing to AppleScript's date handling.
        date_script = (
            f'tell application "Reminders"\n'
            f'  set r to reminder id "{_esc(rid)}"\n'
            f'  set remind me date of r to date "{_esc(due)}"\n'
            f'end tell'
        )
        await _osascript(date_script)

    return {"id": rid, "title": title, "due": due, "list": list_name}


async def complete_reminder(args: dict) -> Any:
    rid = args.get("reminder_id")
    if not rid:
        return {"error": "reminder_id is required"}
    script = (
        'tell application "Reminders"\n'
        f'  set completed of reminder id "{_esc(rid)}" to true\n'
        'end tell'
    )
    rc, _, err = await _osascript(script)
    if rc != 0:
        return {"error": err.strip()}
    return {"ok": True, "id": rid}


TOOLS = [
    Tool(
        name="list_reminders",
        description="List open (incomplete) Apple Reminders. Optionally filter to one list by name.",
        input_schema={
            "type": "object",
            "properties": {
                "list_name": {"type": "string", "description": "Optional Reminders list to filter to"},
            },
        },
        impl=list_reminders,
        tier=Tier.READ,
    ),
    Tool(
        name="create_reminder",
        description=(
            "Create a new Apple Reminder. Use for tasks, follow-ups, or time-based nudges. "
            "`due` accepts AppleScript-compatible date strings (e.g., 'tomorrow 5:00 PM', "
            "'April 30, 2026 09:00 AM')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": {"type": "string", "description": "Optional due date/time"},
                "list_name": {"type": "string", "description": "Optional target list"},
                "notes": {"type": "string", "description": "Optional body notes"},
            },
            "required": ["title"],
        },
        impl=create_reminder,
        tier=Tier.SEND,
    ),
    Tool(
        name="complete_reminder",
        description="Mark an Apple Reminder as completed. `reminder_id` comes from list_reminders.",
        input_schema={
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string"},
            },
            "required": ["reminder_id"],
        },
        impl=complete_reminder,
        tier=Tier.SEND,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    for t in TOOLS:
        registry.add(t)
